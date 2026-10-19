//! Graphics UI (M10.2): the Program-monitor overlay for graphic clips and the graphic sections of
//! the Properties / Essential Graphics panels.
//!
//! Monitor overlay:
//! - **Type tool (T):** click on empty picture → a new text layer (in the selected graphic clip
//!   under the playhead, else a new graphic clip) with a caret; click on a text layer → caret at
//!   the click. Typing edits the text (one undo step per typing session); arrows, Home/End,
//!   Shift-selection, ⌘A, ⌘C/⌘X/⌘V, Return (new line) and Esc (stop editing) work as in a text
//!   field. Double-clicking a text layer with the Selection tool also edits it.
//! - **Selection tool:** click selects a layer (bounding box with handles); drag moves it; drag a
//!   corner handle scales it.
//! - **Rectangle / Ellipse tools:** drag out a shape. **Pen tool:** click points; click the first
//!   point again (or press Return) to close the path; Esc cancels.
//!
//! All edits go through `graphics.*` commands. Automation ids: `program.layer.<clip>.<layer>`,
//! `program.layer.<clip>.<layer>.handle.<n>`, `program.textEdit`, `graphics.*` in the panels.

use egui::{Align2, Color32, Pos2, Rect, Sense, Stroke, StrokeKind, pos2, vec2};
use lumiere_codecbase_geom::{Affine, Vec2};
use lumiere_codecbase_project::graphic::{self, LayerContent, LayerSpec, eval_layer, layer_display_name, layer_indices};
use lumiere_codecbase_project::{ClipId, EffectInstance, ItemKind, ParamValue, TrackItem};
use lumiere_codecbase_render::graphic_clip::{layer_local_bounds, layer_matrix, text_layout};
use lumiere_codecbase_time::Tick;
use serde_json::{Value, json};

use crate::LumiereEditApp;
use crate::icons::{self, Icon};
use crate::state::{GfxEdit, Tool};
use crate::theme::Tokens;

/// A graphic layer visible at the playhead, with its layer→screen transform.
pub struct LayerView {
    pub clip: ClipId,
    pub layer: usize,
    pub spec: LayerSpec,
    pub to_screen: Affine,
    /// Graphic canvas → screen (the clip's Motion and the monitor fit).
    pub canvas_to_screen: Affine,
    pub local: [f32; 4],
}

impl LayerView {
    pub fn quad(&self) -> [Pos2; 4] {
        let b = self.local;
        [(b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])].map(|(x, y)| sp(&self.to_screen, x, y))
    }
    fn to_local(&self, p: Pos2) -> Option<(f32, f32)> {
        let v = self.to_screen.inverse()?.apply(Vec2::new(p.x as f64, p.y as f64));
        Some((v.x as f32, v.y as f32))
    }
    fn hit(&self, p: Pos2) -> bool {
        let pad = 4.0 / screen_scale(&self.to_screen).max(1e-6);
        self.to_local(p).is_some_and(|(x, y)| x >= self.local[0] - pad && x <= self.local[2] + pad && y >= self.local[1] - pad && y <= self.local[3] + pad)
    }
    fn is_text(&self) -> bool {
        matches!(self.spec.content, LayerContent::Text(_))
    }
}

fn sp(a: &Affine, x: f32, y: f32) -> Pos2 {
    let v = a.apply(Vec2::new(x as f64, y as f64));
    pos2(v.x as f32, v.y as f32)
}

fn screen_scale(a: &Affine) -> f32 {
    (a.a * a.d - a.b * a.c).abs().sqrt() as f32
}

/// Whether a clip id is a graphic clip in the active sequence.
pub fn is_graphic_clip(app: &LumiereEditApp, c: ClipId) -> bool {
    app.session
        .active_sequence()
        .and_then(|q| q.find_item(c))
        .and_then(|(_, it)| app.session.project.item(it.item))
        .is_some_and(|p| matches!(p.kind, ItemKind::Graphic { .. }))
}

/// The selected graphic clip, if any.
pub fn selected_graphic(app: &LumiereEditApp) -> Option<(ClipId, TrackItem)> {
    let q = app.session.active_sequence()?;
    app.session.state.selection.iter().copied().find(|c| is_graphic_clip(app, *c)).and_then(|c| q.find_item(c).map(|(_, it)| (c, it.clone())))
}

/// Layers of the graphic clips visible at the playhead, front-most first.
pub fn visible_layers(app: &LumiereEditApp, pic: Rect, frame: (u32, u32)) -> Vec<LayerView> {
    let Some(seq) = app.session.active_sequence() else { return Vec::new() };
    let t = app.session.playhead();
    let k = pic.width() as f64 / frame.0.max(1) as f64;
    let screen = Affine::translate(pic.min.x as f64, pic.min.y as f64).then_apply(&Affine::scale(k, k));
    let mut out = Vec::new();
    for tr in seq.video_tracks.iter().rev() {
        if !tr.enabled {
            continue;
        }
        let Some(it) = tr.item_at(t) else { continue };
        if !it.enabled {
            continue;
        }
        let Some(ItemKind::Graphic { width, height, .. }) = app.session.project.item(it.item).map(|p| &p.kind) else { continue };
        let size = (*width, *height);
        let mt = it.source_time_at(t);
        let base = screen.then_apply(&lumiere_codecbase_render::motion_matrix(seq, it, size, mt));
        let idx = layer_indices(&it.effects);
        for (li, &ei) in idx.iter().enumerate().rev() {
            if let Some(spec) = eval_layer(&it.effects[ei], mt, size) {
                let local = layer_local_bounds(&spec);
                out.push(LayerView { clip: it.id, layer: li, to_screen: base.then_apply(&layer_matrix(&spec)), canvas_to_screen: base, spec, local });
            }
        }
    }
    out
}

/// Screen → graphic-canvas mapping for new layers: the selected graphic clip under the playhead
/// (its Motion applies), else the sequence frame.
fn canvas_target(app: &LumiereEditApp, pic: Rect, frame: (u32, u32)) -> (Option<ClipId>, Affine) {
    let k = pic.width() as f64 / frame.0.max(1) as f64;
    let screen = Affine::translate(pic.min.x as f64, pic.min.y as f64).then_apply(&Affine::scale(k, k));
    let t = app.session.playhead();
    if let Some((c, it)) = selected_graphic(app)
        && it.range().contains(t)
        && let Some(seq) = app.session.active_sequence()
        && let Some(ItemKind::Graphic { width, height, .. }) = app.session.project.item(it.item).map(|p| &p.kind)
    {
        let m = screen.then_apply(&lumiere_codecbase_render::motion_matrix(seq, &it, (*width, *height), it.source_time_at(t)));
        return (Some(c), m);
    }
    (None, screen)
}

fn to_canvas(m: &Affine, p: Pos2) -> Vec2 {
    m.inverse().map(|i| i.apply(Vec2::new(p.x as f64, p.y as f64))).unwrap_or_default()
}

fn text_of(spec: &LayerSpec) -> Option<&lumiere_codecbase_project::graphic::TextProps> {
    match &spec.content {
        LayerContent::Text(t) => Some(t),
        _ => None,
    }
}

fn prev_boundary(s: &str, i: usize) -> usize {
    s[..i.min(s.len())].char_indices().next_back().map_or(0, |(b, _)| b)
}
fn next_boundary(s: &str, i: usize) -> usize {
    let i = i.min(s.len());
    s[i..].chars().next().map_or(i, |c| i + c.len_utf8())
}

#[derive(Clone, Copy, Debug)]
enum DragKind {
    Move,
    /// A corner handle: scales about the layer's anchor.
    Scale,
    NewShape,
    TextSelect,
}

#[derive(Clone, Copy, Debug)]
struct DragState {
    kind: DragKind,
    clip: ClipId,
    layer: usize,
    start: Pos2,
}

const TEXT_EDIT_ID: &str = "gfx-text-edit";

fn end_edit(app: &mut LumiereEditApp, ui: &egui::Ui) {
    app.ui.gfx_edit = None;
    ui.memory_mut(|m| m.surrender_focus(egui::Id::new(TEXT_EDIT_ID)));
}

/// Draw the overlay and handle graphics tools on the Program monitor picture `pic`.
pub fn monitor_overlay(app: &mut LumiereEditApp, ui: &mut egui::Ui, pic: Rect, frame: (u32, u32)) {
    let t = app.tokens;
    let views = visible_layers(app, pic, frame);
    let tool = app.ui.tool;
    let accent = t.accent;
    let painter = ui.painter().with_clip_rect(pic.expand(12.0));
    for v in &views {
        let q = v.quad();
        let r = Rect::from_points(&q);
        let name = layer_display_name_view(app, v);
        app.auto.add(&format!("program.layer.{}.{}", v.clip.0, v.layer), r, &name);
    }
    let sel_clip = selected_graphic(app).map(|(c, _)| c);
    let sel_layers = app.session.state.graphic_layers.clone();
    let editing = app.ui.gfx_edit.clone();
    // ---- boxes and handles
    let graphics_tool = matches!(tool, Tool::Selection | Tool::Type | Tool::Rectangle | Tool::Ellipse | Tool::Pen);
    if graphics_tool {
        for v in views.iter().filter(|v| Some(v.clip) == sel_clip) {
            let q = v.quad();
            let selected = sel_layers.contains(&v.layer);
            let is_edit = editing.as_ref().is_some_and(|e| e.clip == v.clip.0 && e.layer == v.layer);
            let col = if selected || is_edit { accent } else { Color32::from_white_alpha(70) };
            painter.add(egui::Shape::closed_line(q.to_vec(), Stroke::new(1.0, col)));
            if selected && !is_edit {
                for (n, c) in q.iter().enumerate() {
                    let hr = Rect::from_center_size(*c, vec2(7.0, 7.0));
                    painter.rect_filled(hr, 0.0, Color32::WHITE);
                    painter.rect_stroke(hr, 0.0, Stroke::new(1.0, accent), StrokeKind::Middle);
                    app.auto.add(&format!("program.layer.{}.{}.handle.{n}", v.clip.0, v.layer), hr.expand(3.0), "scale handle");
                }
                for i in 0..4 {
                    let m = q[i] + (q[(i + 1) % 4] - q[i]) * 0.5;
                    painter.rect_filled(Rect::from_center_size(m, vec2(5.0, 5.0)), 0.0, Color32::WHITE);
                }
                // anchor point
                let a = sp(&v.to_screen, v.spec.transform.anchor.x as f32, v.spec.transform.anchor.y as f32);
                painter.circle_stroke(a, 4.0, Stroke::new(1.0, accent));
                painter.line_segment([a - vec2(6.0, 0.0), a + vec2(6.0, 0.0)], Stroke::new(1.0, accent));
                painter.line_segment([a - vec2(0.0, 6.0), a + vec2(0.0, 6.0)], Stroke::new(1.0, accent));
            }
        }
    }
    let id = egui::Id::new("gfx-overlay");
    let resp = ui.interact(pic, id, if graphics_tool { Sense::click_and_drag() } else { Sense::hover() });
    let hover = resp.hover_pos();
    let mut actions: Vec<(String, Value)> = Vec::new();
    let drag_id = id.with("drag");
    let mut drag: Option<DragState> = ui.data(|d| d.get_temp(drag_id));

    // cursors
    if let Some(p) = hover {
        match tool {
            Tool::Type => ui.ctx().set_cursor_icon(egui::CursorIcon::Text),
            Tool::Rectangle | Tool::Ellipse | Tool::Pen => ui.ctx().set_cursor_icon(egui::CursorIcon::Crosshair),
            Tool::Selection => {
                if editing.as_ref().is_some_and(|e| views.iter().any(|v| v.clip.0 == e.clip && v.layer == e.layer && v.hit(p))) {
                    ui.ctx().set_cursor_icon(egui::CursorIcon::Text);
                } else if views.iter().any(|v| v.hit(p)) {
                    ui.ctx().set_cursor_icon(egui::CursorIcon::Move);
                }
            }
            _ => {}
        }
    }

    // ---- text editing
    if let Some(ed) = editing.clone() {
        match views.iter().find(|v| v.clip.0 == ed.clip && v.layer == ed.layer && v.is_text()) {
            Some(v) => text_edit(app, ui, v, ed, &resp, &mut actions),
            None => end_edit(app, ui),
        }
    }
    let editing = app.ui.gfx_edit.clone();
    let edit_view = editing.as_ref().and_then(|e| views.iter().find(|v| v.clip.0 == e.clip && v.layer == e.layer));

    // ---- presses
    if graphics_tool && resp.drag_started() {
        // where the button went down (a drag starts only after the pointer has moved a little)
        let p = ui.input(|i| i.pointer.press_origin()).or(resp.interact_pointer_pos()).unwrap_or(pic.center());
        let shift = ui.input(|i| i.modifiers.shift);
        let in_edit = edit_view.is_some_and(|v| v.hit(p));
        drag = None;
        if in_edit {
            if let (Some(v), Some(mut ed)) = (edit_view, editing.clone()) {
                let l = text_layout(text_of(&v.spec).expect("text"));
                if let Some((x, y)) = v.to_local(p) {
                    ed.caret = l.hit(x, y);
                    if !shift {
                        ed.anchor = ed.caret;
                    }
                    app.ui.gfx_edit = Some(ed);
                }
                drag = Some(DragState { kind: DragKind::TextSelect, clip: v.clip, layer: v.layer, start: p });
            }
        } else {
            match tool {
                Tool::Selection | Tool::Type => {
                    // handles of the selected layers first
                    let handle = views
                        .iter()
                        .filter(|v| Some(v.clip) == sel_clip && sel_layers.contains(&v.layer))
                        .find_map(|v| v.quad().iter().position(|c| (*c - p).length() <= 7.0).map(|_| (v.clip, v.layer)));
                    if let Some((c, l)) = handle {
                        drag = Some(DragState { kind: DragKind::Scale, clip: c, layer: l, start: p });
                    } else if tool == Tool::Selection
                        && let Some(v) = views.iter().find(|v| v.hit(p))
                    {
                        if editing.is_some() {
                            end_edit(app, ui);
                        }
                        if !(Some(v.clip) == sel_clip && sel_layers.contains(&v.layer)) {
                            actions.push(("graphics.selectLayer".into(), json!({"clip": v.clip.0, "layers": [v.layer]})));
                        }
                        drag = Some(DragState { kind: DragKind::Move, clip: v.clip, layer: v.layer, start: p });
                    }
                }
                Tool::Rectangle | Tool::Ellipse => {
                    drag = Some(DragState { kind: DragKind::NewShape, clip: ClipId(0), layer: 0, start: p });
                }
                _ => {}
            }
        }
        ui.data_mut(|dd| {
            if let Some(d) = drag {
                dd.insert_temp(drag_id, d);
            } else {
                dd.remove::<DragState>(drag_id);
            }
        });
    }
    // ---- drag feedback / commit
    if let Some(d) = drag {
        let cur = resp.interact_pointer_pos().or(hover).unwrap_or(d.start);
        let v = views.iter().find(|v| v.clip == d.clip && v.layer == d.layer);
        // Snap in Program Monitor (hold ⌘/Ctrl to move freely)
        let snap_off = match (d.kind, v) {
            (DragKind::Move, Some(v)) if !ui.input(|i| i.modifiers.command) => {
                crate::panels::monitor_view::snap_move(app, pic, frame, Rect::from_points(&v.quad()), cur - d.start)
            }
            _ => (cur - d.start, Vec::new()),
        };
        match d.kind {
            DragKind::Move => {
                if let Some(v) = v {
                    let off = snap_off.0;
                    painter.add(egui::Shape::closed_line(v.quad().iter().map(|q| *q + off).collect(), Stroke::new(1.0, Color32::WHITE)));
                    crate::panels::monitor_view::draw_snap_lines(&painter, pic, &snap_off.1);
                }
            }
            DragKind::Scale => {
                if let Some(v) = v {
                    let a = sp(&v.to_screen, v.spec.transform.anchor.x as f32, v.spec.transform.anchor.y as f32);
                    let f = ((cur - a).length() / (d.start - a).length().max(1.0)).max(0.01);
                    painter.add(egui::Shape::closed_line(v.quad().iter().map(|q| a + (*q - a) * f).collect(), Stroke::new(1.0, Color32::WHITE)));
                }
            }
            DragKind::NewShape => {
                let r = Rect::from_two_pos(d.start, cur);
                if tool == Tool::Ellipse {
                    painter.add(egui::Shape::ellipse_stroke(r.center(), r.size() / 2.0, Stroke::new(1.0, Color32::WHITE)));
                } else {
                    painter.rect_stroke(r, 0.0, Stroke::new(1.0, Color32::WHITE), StrokeKind::Middle);
                }
            }
            DragKind::TextSelect => {
                if let (Some(v), Some(mut ed)) = (v, app.ui.gfx_edit.clone()) {
                    let l = text_layout(text_of(&v.spec).expect("text"));
                    if let Some((x, y)) = v.to_local(cur) {
                        ed.caret = l.hit(x, y);
                        app.ui.gfx_edit = Some(ed);
                    }
                }
            }
        }
        if resp.drag_stopped() {
            ui.data_mut(|dd| dd.remove::<DragState>(drag_id));
            let moved = (cur - d.start).length() > 2.0;
            match d.kind {
                DragKind::Move if moved => {
                    if let Some(v) = v {
                        let lin = Affine { e: 0.0, f: 0.0, ..v.canvas_to_screen };
                        let off = snap_off.0;
                        let delta = lin.inverse().map(|i| i.apply(Vec2::new(off.x as f64, off.y as f64))).unwrap_or_default();
                        let p0 = v.spec.transform.position;
                        actions.push((
                            "graphics.set".into(),
                            json!({"clip": v.clip.0, "layer": v.layer, "props": {"position": [p0.x + delta.x, p0.y + delta.y]}}),
                        ));
                    }
                }
                DragKind::Scale if moved => {
                    if let Some(v) = v {
                        let a = sp(&v.to_screen, v.spec.transform.anchor.x as f32, v.spec.transform.anchor.y as f32);
                        let f = ((cur - a).length() / (d.start - a).length().max(1.0)).max(0.01) as f64;
                        let s = v.spec.transform.scale;
                        actions.push((
                            "graphics.set".into(),
                            json!({"clip": v.clip.0, "layer": v.layer, "props": {"scale": s.y * 100.0 * f, "scale_width": s.x * 100.0 * f}}),
                        ));
                    }
                }
                DragKind::NewShape if moved => {
                    let (into, m) = canvas_target(app, pic, frame);
                    let (a, b) = (to_canvas(&m, d.start), to_canvas(&m, cur));
                    let c = Vec2::new((a.x + b.x) / 2.0, (a.y + b.y) / 2.0);
                    let mut p = json!({
                        "shape": if tool == Tool::Ellipse { "ellipse" } else { "rectangle" },
                        "position": [c.x, c.y],
                        "size": [(b.x - a.x).abs(), (b.y - a.y).abs()],
                    });
                    if let Some(cl) = into {
                        p["clip"] = json!(cl.0);
                    }
                    actions.push(("graphics.newShape".into(), p));
                }
                _ => {}
            }
        }
    }
    // ---- clicks
    if graphics_tool && resp.clicked() && drag.is_none_or(|d| !matches!(d.kind, DragKind::TextSelect)) {
        let p = resp.interact_pointer_pos().unwrap_or(pic.center());
        let in_edit = edit_view.is_some_and(|v| v.hit(p));
        match tool {
            _ if in_edit => {}
            Tool::Type => {
                if let Some(v) = views.iter().find(|v| v.is_text() && v.hit(p)) {
                    start_edit_at(app, ui, v, p, &mut actions);
                } else {
                    end_edit(app, ui);
                    let (into, m) = canvas_target(app, pic, frame);
                    let c = to_canvas(&m, p);
                    let size = 100.0 * frame.1 as f64 / 1080.0;
                    let mut prm = json!({"text": "", "position": [c.x, c.y], "size": size.round()});
                    if let Some(cl) = into {
                        prm["clip"] = json!(cl.0);
                    }
                    actions.push(("graphics.newText#edit".into(), prm));
                }
            }
            Tool::Selection => {
                if resp.double_clicked() {
                    if let Some(v) = views.iter().find(|v| v.is_text() && v.hit(p)) {
                        start_edit_at(app, ui, v, p, &mut actions);
                    }
                } else if let Some(v) = views.iter().find(|v| v.hit(p)) {
                    if app.ui.gfx_edit.is_some() {
                        end_edit(app, ui);
                    }
                    actions.push(("graphics.selectLayer".into(), json!({"clip": v.clip.0, "layers": [v.layer]})));
                } else if app.ui.gfx_edit.is_some() {
                    end_edit(app, ui);
                } else if sel_clip.is_some() && !sel_layers.is_empty() {
                    app.session.state.graphic_layers.clear();
                }
            }
            Tool::Pen => {
                let (_, m) = canvas_target(app, pic, frame);
                let c = to_canvas(&m, p);
                let close = app.ui.pen_points.len() >= 3
                    && app.ui.pen_points.first().is_some_and(|f| {
                        let fs = m.apply(Vec2::new(f[0], f[1]));
                        (pos2(fs.x as f32, fs.y as f32) - p).length() <= 8.0
                    });
                if close {
                    finish_pen(app, pic, frame, &mut actions);
                } else {
                    app.ui.pen_points.push([c.x, c.y]);
                }
            }
            _ => {}
        }
    }
    // ---- pen path in progress
    if tool == Tool::Pen && !app.ui.pen_points.is_empty() {
        let (_, m) = canvas_target(app, pic, frame);
        let pts: Vec<Pos2> = app.ui.pen_points.iter().map(|q| m.apply(Vec2::new(q[0], q[1]))).map(|v| pos2(v.x as f32, v.y as f32)).collect();
        painter.add(egui::Shape::line(pts.clone(), Stroke::new(1.0, accent)));
        if let (Some(last), Some(h)) = (pts.last(), hover) {
            painter.line_segment([*last, h], Stroke::new(1.0, accent.gamma_multiply(0.5)));
        }
        for (i, q) in pts.iter().enumerate() {
            painter.rect_filled(Rect::from_center_size(*q, vec2(6.0, 6.0)), 0.0, if i == 0 { accent } else { Color32::WHITE });
        }
        let (enter, esc) = ui.input(|i| (i.key_pressed(egui::Key::Enter), i.key_pressed(egui::Key::Escape)));
        if enter && app.ui.pen_points.len() >= 3 {
            finish_pen(app, pic, frame, &mut actions);
        } else if esc {
            app.ui.pen_points.clear();
        }
    } else if tool != Tool::Pen && !app.ui.pen_points.is_empty() {
        app.ui.pen_points.clear();
    }
    // ---- caret / selection drawing
    if let Some(ed) = app.ui.gfx_edit.clone()
        && let Some(v) = views.iter().find(|v| v.clip.0 == ed.clip && v.layer == ed.layer)
        && let Some(tp) = text_of(&v.spec)
    {
        let l = text_layout(tp);
        let caret = ed.caret.min(tp.text.len());
        for r in l.selection_rects(ed.anchor.min(tp.text.len()), caret) {
            let q = [(r[0], r[1]), (r[2], r[1]), (r[2], r[3]), (r[0], r[3])].map(|(x, y)| sp(&v.to_screen, x, y));
            painter.add(egui::Shape::convex_polygon(q.to_vec(), Color32::from_rgba_unmultiplied(80, 140, 255, 90), Stroke::NONE));
        }
        let (x, base, li) = l.caret(caret);
        let line = &l.lines[li.min(l.lines.len() - 1)];
        let (a, b) = (sp(&v.to_screen, x, base - line.ascent), sp(&v.to_screen, x, base + line.descent));
        painter.line_segment([a, b], Stroke::new(1.5, Color32::WHITE));
        painter.line_segment([a + vec2(1.0, 0.0), b + vec2(1.0, 0.0)], Stroke::new(0.5, Color32::BLACK));
        let er = Rect::from_points(&v.quad()).union(Rect::from_two_pos(a, b));
        app.auto.add("program.textEdit", er, &tp.text);
        ui.ctx().output_mut(|o| {
            o.ime = Some(egui::output::IMEOutput {
                purpose: Default::default(),
                rect: er,
                cursor_rect: Rect::from_two_pos(a, b),
                should_interrupt_composition: false,
            })
        });
    }
    // ---- run
    for (cmd, p) in actions {
        let (cmd, then_edit) = match cmd.strip_suffix("#edit") {
            Some(c) => (c.to_string(), true),
            None => (cmd, false),
        };
        match app.session.execute(&cmd, p) {
            Ok(r) => {
                if then_edit && let (Some(c), Some(l)) = (r["clip"].as_u64(), r["layer"].as_u64()) {
                    app.ui.gfx_edit = Some(GfxEdit { clip: c, layer: l as usize, caret: 0, anchor: 0 });
                    app.ui.focused = crate::dock::PanelKind::Program;
                }
            }
            Err(e) => app.ui.status = e.to_string(),
        }
    }
}

fn layer_display_name_view(app: &LumiereEditApp, v: &LayerView) -> String {
    app.session
        .active_sequence()
        .and_then(|q| q.find_item(v.clip))
        .and_then(|(_, it)| layer_indices(&it.effects).get(v.layer).map(|&e| layer_display_name(&it.effects[e], v.layer)))
        .unwrap_or_default()
}

fn finish_pen(app: &mut LumiereEditApp, pic: Rect, frame: (u32, u32), actions: &mut Vec<(String, Value)>) {
    let pts = std::mem::take(&mut app.ui.pen_points);
    if pts.len() < 3 {
        return;
    }
    let n = pts.len() as f64;
    let (cx, cy) = (pts.iter().map(|p| p[0]).sum::<f64>() / n, pts.iter().map(|p| p[1]).sum::<f64>() / n);
    let (into, _) = canvas_target(app, pic, frame);
    let mut p = json!({
        "shape": "path",
        "position": [cx, cy],
        "points": pts.iter().map(|q| [q[0] - cx, q[1] - cy]).collect::<Vec<_>>(),
    });
    if let Some(c) = into {
        p["clip"] = json!(c.0);
    }
    actions.push(("graphics.newShape".into(), p));
}

fn start_edit_at(app: &mut LumiereEditApp, ui: &egui::Ui, v: &LayerView, p: Pos2, actions: &mut Vec<(String, Value)>) {
    let Some(tp) = text_of(&v.spec) else { return };
    let l = text_layout(tp);
    let caret = v.to_local(p).map_or(tp.text.len(), |(x, y)| l.hit(x, y));
    app.ui.gfx_edit = Some(GfxEdit { clip: v.clip.0, layer: v.layer, caret, anchor: caret });
    app.ui.focused = crate::dock::PanelKind::Program;
    let _ = ui;
    actions.push(("graphics.selectLayer".into(), json!({"clip": v.clip.0, "layers": [v.layer]})));
}

/// Keyboard handling while a text layer is being edited.
fn text_edit(app: &mut LumiereEditApp, ui: &mut egui::Ui, v: &LayerView, mut ed: GfxEdit, resp: &egui::Response, actions: &mut Vec<(String, Value)>) {
    let _ = resp;
    let eid = egui::Id::new(TEXT_EDIT_ID);
    // a real (non-clickable) widget holds keyboard focus so shortcuts pause while typing
    let fr = ui.interact(Rect::from_points(&v.quad()), eid, Sense::focusable_noninteractive());
    fr.widget_info(|| egui::WidgetInfo::labeled(egui::WidgetType::TextEdit, true, "Text layer"));
    ui.memory_mut(|m| {
        m.request_focus(eid);
        m.set_focus_lock_filter(eid, egui::EventFilter { tab: true, horizontal_arrows: true, vertical_arrows: true, escape: true });
    });
    let Some(tp) = text_of(&v.spec) else { return };
    let mut text = tp.text.clone();
    ed.caret = ed.caret.min(text.len());
    ed.anchor = ed.anchor.min(text.len());
    let l = text_layout(tp);
    let events = ui.input(|i| i.events.clone());
    let mut changed = false;
    let mut stop = false;
    let sel = |ed: &GfxEdit| (ed.caret.min(ed.anchor), ed.caret.max(ed.anchor));
    let delete_sel = |text: &mut String, ed: &mut GfxEdit| -> bool {
        let (a, b) = sel(ed);
        if a == b {
            return false;
        }
        text.replace_range(a..b, "");
        ed.caret = a;
        ed.anchor = a;
        true
    };
    for ev in events {
        match ev {
            egui::Event::Text(s) | egui::Event::Paste(s) => {
                let s: String = s.chars().filter(|c| !c.is_control() || *c == '\n').collect();
                if s.is_empty() {
                    continue;
                }
                delete_sel(&mut text, &mut ed);
                text.insert_str(ed.caret, &s);
                ed.caret += s.len();
                ed.anchor = ed.caret;
                changed = true;
            }
            egui::Event::Copy | egui::Event::Cut => {
                let (a, b) = sel(&ed);
                if a < b {
                    ui.ctx().copy_text(text[a..b].to_string());
                    if matches!(ev, egui::Event::Cut) {
                        changed |= delete_sel(&mut text, &mut ed);
                    }
                }
            }
            egui::Event::Key { key, pressed: true, modifiers, .. } => {
                let shift = modifiers.shift;
                let mut moved = None;
                match key {
                    egui::Key::Escape => stop = true,
                    egui::Key::Enter => {
                        delete_sel(&mut text, &mut ed);
                        text.insert(ed.caret, '\n');
                        ed.caret += 1;
                        ed.anchor = ed.caret;
                        changed = true;
                    }
                    egui::Key::Backspace => {
                        if !delete_sel(&mut text, &mut ed) && ed.caret > 0 {
                            let p = if modifiers.alt || modifiers.command { word_left(&text, ed.caret) } else { prev_boundary(&text, ed.caret) };
                            text.replace_range(p..ed.caret, "");
                            ed.caret = p;
                            ed.anchor = p;
                        }
                        changed = true;
                    }
                    egui::Key::Delete => {
                        if !delete_sel(&mut text, &mut ed) && ed.caret < text.len() {
                            let n = next_boundary(&text, ed.caret);
                            text.replace_range(ed.caret..n, "");
                        }
                        changed = true;
                    }
                    egui::Key::A if modifiers.command => {
                        ed.anchor = 0;
                        ed.caret = text.len();
                    }
                    egui::Key::ArrowLeft => {
                        let (a, _) = sel(&ed);
                        moved = Some(if !shift && ed.caret != ed.anchor {
                            a
                        } else if modifiers.command {
                            l.lines[l.caret(ed.caret).2].range.start
                        } else if modifiers.alt {
                            word_left(&text, ed.caret)
                        } else {
                            prev_boundary(&text, ed.caret)
                        });
                    }
                    egui::Key::ArrowRight => {
                        let (_, b) = sel(&ed);
                        moved = Some(if !shift && ed.caret != ed.anchor {
                            b
                        } else if modifiers.command {
                            l.lines[l.caret(ed.caret).2].range.end
                        } else if modifiers.alt {
                            word_right(&text, ed.caret)
                        } else {
                            next_boundary(&text, ed.caret)
                        });
                    }
                    egui::Key::ArrowUp | egui::Key::ArrowDown => {
                        let (x, _, li) = l.caret(ed.caret);
                        let target = if key == egui::Key::ArrowUp { li.checked_sub(1) } else { Some(li + 1).filter(|n| *n < l.lines.len()) };
                        moved = Some(match target {
                            Some(n) => l.hit(x, l.lines[n].baseline),
                            None if key == egui::Key::ArrowUp => 0,
                            None => text.len(),
                        });
                    }
                    egui::Key::Home => moved = Some(l.lines[l.caret(ed.caret).2].range.start),
                    egui::Key::End => moved = Some(l.lines[l.caret(ed.caret).2].range.end),
                    _ => {}
                }
                if let Some(m) = moved {
                    ed.caret = m.min(text.len());
                    if !shift {
                        ed.anchor = ed.caret;
                    }
                }
            }
            _ => {}
        }
    }
    if changed && text != tp.text {
        actions.push(("graphics.setText".into(), json!({"clip": ed.clip, "layer": ed.layer, "text": text, "merge": true})));
    }
    if stop {
        end_edit(app, ui);
    } else {
        app.ui.gfx_edit = Some(ed);
    }
}

fn word_left(s: &str, i: usize) -> usize {
    let mut j = i;
    let b: Vec<(usize, char)> = s[..i].char_indices().collect();
    let mut k = b.len();
    while k > 0 && b[k - 1].1.is_whitespace() {
        k -= 1;
    }
    while k > 0 && !b[k - 1].1.is_whitespace() {
        k -= 1;
    }
    if k < b.len() {
        j = b[k].0;
    }
    if k == 0 { 0 } else { j }
}

fn word_right(s: &str, i: usize) -> usize {
    let mut it = s[i..].char_indices().peekable();
    while it.peek().is_some_and(|(_, c)| c.is_whitespace()) {
        it.next();
    }
    while it.peek().is_some_and(|(_, c)| !c.is_whitespace()) {
        it.next();
    }
    it.peek().map_or(s.len(), |(b, _)| i + b)
}

// ---------------------------------------------------------------------------------------------
// Properties / Essential Graphics
// ---------------------------------------------------------------------------------------------

const ROW: f32 = 28.0;

fn pv(e: &EffectInstance, id: &str, mt: Tick) -> ParamValue {
    e.params.get(id).map(|p| p.value_at(mt)).or_else(|| e.def().and_then(|d| d.param(id)).map(|d| d.default.clone())).unwrap_or(ParamValue::Float(0.0))
}

fn color32(c: [f32; 4]) -> Color32 {
    let q = |v: f32| (v.clamp(0.0, 1.0) * 255.0).round() as u8;
    Color32::from_rgba_unmultiplied(q(c[0]), q(c[1]), q(c[2]), q(c[3]))
}

struct Ctx<'a> {
    clip: ClipId,
    layer: usize,
    e: &'a EffectInstance,
    mt: Tick,
    t: Tokens,
    actions: Vec<(String, Value)>,
    autos: Vec<(String, Rect, String)>,
    /// Characters selected with the Type tool in this layer: character properties apply to them.
    range: Option<(usize, usize)>,
}

/// Character properties a Type-tool selection can carry, with their `graphics.setCharStyle` names.
fn char_style_key(id: &str) -> Option<&'static str> {
    Some(match id {
        "font" => "font",
        "font_style" => "fontStyle",
        "size" => "size",
        "fill_color" => "color",
        "faux_bold" => "bold",
        "faux_italic" => "italic",
        "underline" => "underline",
        "tracking" => "tracking",
        "baseline_shift" => "baselineShift",
        "caps" => "caps",
        _ => return None,
    })
}

impl Ctx<'_> {
    fn set(&mut self, id: &str, v: Value) {
        if let (Some((a, b)), Some(k)) = (self.range, char_style_key(id)) {
            self.actions.push(("graphics.setCharStyle".into(), json!({"clip": self.clip.0, "layer": self.layer, "start": a, "end": b, "style": {k: v}})));
            return;
        }
        self.actions.push(("graphics.set".into(), json!({"clip": self.clip.0, "layer": self.layer, "props": {id: v}})));
    }
    fn auto(&mut self, id: &str, r: Rect, label: &str) {
        self.autos.push((format!("graphics.prop.{id}"), r, label.to_string()));
    }
    fn row(&self, ui: &mut egui::Ui, label: &str) -> (Rect, egui::Ui) {
        let (r, _) = ui.allocate_exact_size(vec2(ui.available_width(), ROW), Sense::hover());
        ui.painter().text(pos2(r.min.x + 18.0, r.center().y), Align2::LEFT_CENTER, label, Tokens::ui(12.0), self.t.text_dim);
        let v = ui.new_child(
            egui::UiBuilder::new()
                .max_rect(Rect::from_min_max(pos2(r.min.x + 130.0, r.min.y + 3.0), pos2(r.max.x - 6.0, r.max.y - 3.0)))
                .layout(egui::Layout::left_to_right(egui::Align::Center)),
        );
        (r, v)
    }
    fn number(&mut self, ui: &mut egui::Ui, label: &str, id: &str, speed: f64, range: (f64, f64), dec: usize, unit: &str) {
        let v = pv(self.e, id, self.mt).as_f64().unwrap_or(0.0);
        let (r, mut vui) = self.row(ui, label);
        let (resp, nv) = crate::widgets::hot_number(&mut vui, egui::Id::new(("gfxp", self.clip.0, self.layer, id)), v, speed, range, dec, unit, &self.t);
        self.auto(id, resp.rect, label);
        let _ = r;
        if let Some(nv) = nv {
            self.set(id, json!(nv));
        }
    }
    fn number_in(&mut self, vui: &mut egui::Ui, id: &str, speed: f64, range: (f64, f64), dec: usize, unit: &str) {
        let v = pv(self.e, id, self.mt).as_f64().unwrap_or(0.0);
        let (resp, nv) = crate::widgets::hot_number(vui, egui::Id::new(("gfxp", self.clip.0, self.layer, id)), v, speed, range, dec, unit, &self.t);
        self.auto(id, resp.rect, id);
        if let Some(nv) = nv {
            self.set(id, json!(nv));
        }
    }
    fn point(&mut self, ui: &mut egui::Ui, label: &str, id: &str) {
        let v = pv(self.e, id, self.mt).as_vec2().unwrap_or_default();
        let (_, mut vui) = self.row(ui, label);
        let (rx, nx) = crate::widgets::hot_number(
            &mut vui,
            egui::Id::new(("gfxp", self.clip.0, self.layer, id, "x")),
            v.x,
            1.0,
            (-100_000.0, 100_000.0),
            1,
            " X",
            &self.t,
        );
        vui.add_space(10.0);
        let (ry, ny) = crate::widgets::hot_number(
            &mut vui,
            egui::Id::new(("gfxp", self.clip.0, self.layer, id, "y")),
            v.y,
            1.0,
            (-100_000.0, 100_000.0),
            1,
            " Y",
            &self.t,
        );
        self.auto(&format!("{id}.x"), rx.rect, label);
        self.auto(&format!("{id}.y"), ry.rect, label);
        if nx.is_some() || ny.is_some() {
            self.set(id, json!([nx.unwrap_or(v.x), ny.unwrap_or(v.y)]));
        }
    }
    fn check(&mut self, vui: &mut egui::Ui, id: &str, label: &str) -> bool {
        let mut b = pv(self.e, id, self.mt).as_bool().unwrap_or(false);
        let r = vui.checkbox(&mut b, label);
        self.auto(id, r.rect, label);
        if r.changed() {
            self.set(id, json!(b));
        }
        b
    }
    fn color(&mut self, vui: &mut egui::Ui, id: &str) {
        let c = pv(self.e, id, self.mt).as_color().unwrap_or([1.0; 4]);
        let mut c32 = color32(c);
        let r = egui::color_picker::color_edit_button_srgba(vui, &mut c32, egui::color_picker::Alpha::Opaque);
        self.auto(id, r.rect, id);
        if r.changed() {
            let [r8, g8, b8, _] = c32.to_srgba_unmultiplied();
            self.set(id, json!(format!("#{r8:02x}{g8:02x}{b8:02x}")));
        }
    }
    fn choice(&mut self, vui: &mut egui::Ui, id: &str, opts: &[&str], width: f32) {
        let cur = match pv(self.e, id, self.mt) {
            ParamValue::Choice(c) => c as usize,
            _ => 0,
        };
        let mut sel = cur;
        let r = egui::ComboBox::from_id_salt(("gfxc", self.clip.0, self.layer, id)).selected_text(opts.get(cur).copied().unwrap_or("")).width(width).show_ui(
            vui,
            |ui| {
                for (i, o) in opts.iter().enumerate() {
                    ui.selectable_value(&mut sel, i, *o);
                }
            },
        );
        self.auto(id, r.response.rect, id);
        if sel != cur {
            self.set(id, json!(sel));
        }
    }
}

fn section(ui: &mut egui::Ui, app: &mut LumiereEditApp, name: &str, t: &Tokens) -> bool {
    ui.add_space(4.0);
    let key = format!("gfx:{name}");
    let open = !app.ui.collapsed_fx.contains(&key);
    let (r, resp) = ui.allocate_exact_size(vec2(ui.available_width(), 30.0), Sense::click());
    ui.painter().line_segment([r.left_top(), r.right_top()], Stroke::new(1.0, t.separator));
    icons::paint(
        ui.painter(),
        Rect::from_center_size(pos2(r.min.x + 6.0, r.center().y), vec2(10.0, 10.0)),
        if open { Icon::ChevronDown } else { Icon::ChevronRight },
        t.text_dim,
    );
    ui.painter().text(pos2(r.min.x + 18.0, r.center().y), Align2::LEFT_CENTER, name, Tokens::semibold(13.0), t.text);
    app.auto.add(&format!("graphics.section.{}", name.replace(' ', "")), r, name);
    if resp.clicked() {
        if open {
            app.ui.collapsed_fx.push(key);
        } else {
            app.ui.collapsed_fx.retain(|k| *k != key);
        }
    }
    open
}

/// A small code-drawn alignment glyph (original artwork: bars against an edge line).
fn align_glyph(p: &egui::Painter, r: Rect, how: &str, c: Color32) {
    let s = Stroke::new(1.2, c);
    let (x0, x1, y0, y1) = (r.min.x + 2.0, r.max.x - 2.0, r.min.y + 2.0, r.max.y - 2.0);
    let (cx, cy) = ((x0 + x1) / 2.0, (y0 + y1) / 2.0);
    let bar = |a: Pos2, b: Pos2| p.rect_filled(Rect::from_two_pos(a, b), 0.0, c);
    match how {
        "left" => {
            p.line_segment([pos2(x0, y0), pos2(x0, y1)], s);
            bar(pos2(x0 + 2.0, y0 + 2.0), pos2(x1, y0 + 5.0));
            bar(pos2(x0 + 2.0, y1 - 5.0), pos2(cx + 1.0, y1 - 2.0));
        }
        "right" => {
            p.line_segment([pos2(x1, y0), pos2(x1, y1)], s);
            bar(pos2(x0, y0 + 2.0), pos2(x1 - 2.0, y0 + 5.0));
            bar(pos2(cx - 1.0, y1 - 5.0), pos2(x1 - 2.0, y1 - 2.0));
        }
        "hcenter" => {
            p.line_segment([pos2(cx, y0), pos2(cx, y1)], s);
            bar(pos2(x0 + 1.0, y0 + 2.0), pos2(x1 - 1.0, y0 + 5.0));
            bar(pos2(cx - 3.0, y1 - 5.0), pos2(cx + 3.0, y1 - 2.0));
        }
        "top" => {
            p.line_segment([pos2(x0, y0), pos2(x1, y0)], s);
            bar(pos2(x0 + 2.0, y0 + 2.0), pos2(x0 + 5.0, y1));
            bar(pos2(x1 - 5.0, y0 + 2.0), pos2(x1 - 2.0, cy + 1.0));
        }
        "bottom" => {
            p.line_segment([pos2(x0, y1), pos2(x1, y1)], s);
            bar(pos2(x0 + 2.0, y0), pos2(x0 + 5.0, y1 - 2.0));
            bar(pos2(x1 - 5.0, cy - 1.0), pos2(x1 - 2.0, y1 - 2.0));
        }
        "vcenter" => {
            p.line_segment([pos2(x0, cy), pos2(x1, cy)], s);
            bar(pos2(x0 + 2.0, y0 + 1.0), pos2(x0 + 5.0, y1 - 1.0));
            bar(pos2(x1 - 5.0, cy - 3.0), pos2(x1 - 2.0, cy + 3.0));
        }
        "dh" => {
            for x in [x0 + 1.0, cx - 1.5, x1 - 4.0] {
                bar(pos2(x, y0 + 3.0), pos2(x + 3.0, y1 - 3.0));
            }
        }
        "dv" => {
            for y in [y0 + 1.0, cy - 1.5, y1 - 4.0] {
                bar(pos2(x0 + 3.0, y), pos2(x1 - 3.0, y + 3.0));
            }
        }
        // paragraph alignment: four text lines
        a @ ("pl" | "pc" | "pr" | "pj") => {
            for (i, w) in [1.0f32, 0.6, 0.85, 0.5].iter().enumerate() {
                let w = if a == "pj" && i < 3 { 1.0 } else { *w } * (x1 - x0);
                let y = y0 + 1.0 + i as f32 * 3.2;
                let xa = match a {
                    "pc" => cx - w / 2.0,
                    "pr" => x1 - w,
                    _ => x0,
                };
                p.line_segment([pos2(xa, y), pos2(xa + w, y)], s);
            }
        }
        _ => {}
    }
}

fn glyph_button(ui: &mut egui::Ui, how: &str, tip: &str, on: bool, t: &Tokens) -> egui::Response {
    let (r, resp) = ui.allocate_exact_size(vec2(24.0, 22.0), Sense::click());
    if on {
        ui.painter().rect_filled(r, 3.0, Color32::from_rgb(0x4b, 0x4b, 0x4b));
    } else if resp.hovered() {
        ui.painter().rect_filled(r, 3.0, t.hover);
    }
    align_glyph(ui.painter(), Rect::from_center_size(r.center(), vec2(16.0, 14.0)), how, if resp.hovered() || on { t.text } else { t.text_dim });
    resp.on_hover_text(tip)
}

fn letter_button(ui: &mut egui::Ui, text: &str, tip: &str, on: bool, t: &Tokens, font: egui::FontId) -> egui::Response {
    let (r, resp) = ui.allocate_exact_size(vec2(26.0, 22.0), Sense::click());
    if on {
        ui.painter().rect_filled(r, 3.0, Color32::from_rgb(0x4b, 0x4b, 0x4b));
    } else if resp.hovered() {
        ui.painter().rect_filled(r, 3.0, t.hover);
    }
    ui.painter().text(r.center(), Align2::CENTER_CENTER, text, font, if on || resp.hovered() { t.text } else { t.text_dim });
    resp.on_hover_text(tip)
}

/// Whether the Properties panel should show the graphic editor.
pub fn graphic_selected(app: &LumiereEditApp) -> bool {
    selected_graphic(app).is_some()
}

/// The graphic editor (Properties panel / Essential Graphics ▸ Edit).
pub fn properties(app: &mut LumiereEditApp, ui: &mut egui::Ui, rect: Rect) {
    let t = app.tokens;
    let Some((clip, it)) = selected_graphic(app) else {
        crate::dock::placeholder(ui, rect, &t, "Select a graphic clip, or use the Type tool (T) on the Program monitor");
        let br = Rect::from_center_size(rect.center() + vec2(0.0, 30.0), vec2(150.0, 26.0));
        let resp = ui.put(br, egui::Button::new("Create new graphic"));
        app.auto.add("graphics.createNew", br, "Create new graphic");
        if resp.clicked()
            && let Err(e) = app.session.execute("graphics.newText", json!({"text": "New Text"}))
        {
            app.ui.status = e.to_string();
        }
        return;
    };
    let ph = app.session.playhead();
    let mt = it.source_time_at(ph.clamp(it.start, it.end() - Tick(1)));
    let idx = layer_indices(&it.effects);
    let sel = app.session.state.graphic_layers.first().copied().filter(|l| *l < idx.len());
    let mut actions: Vec<(String, Value)> = Vec::new();
    let mut autos: Vec<(String, Rect, String)> = Vec::new();
    let mut bui = ui.new_child(egui::UiBuilder::new().max_rect(rect.shrink2(vec2(12.0, 6.0))).id_salt("gfx-props"));
    bui.set_clip_rect(rect);
    egui::ScrollArea::vertical().id_salt("gfx-props-scroll").auto_shrink([false, false]).show(&mut bui, |ui| {
        // header
        let (hr, _) = ui.allocate_exact_size(vec2(ui.available_width(), 30.0), Sense::hover());
        let lc = app.session.prefs.labels.rgb(it.label);
        ui.painter().rect_filled(Rect::from_center_size(pos2(hr.min.x + 8.0, hr.center().y), vec2(12.0, 12.0)), 2.0, Color32::from_rgb(lc[0], lc[1], lc[2]));
        ui.painter().text(pos2(hr.min.x + 22.0, hr.center().y), Align2::LEFT_CENTER, &it.name, Tokens::semibold(12.5), t.text);
        // ---- layers (front first)
        if section(ui, app, "Layers", &t) {
            ui.horizontal(|ui| {
                ui.add_space(14.0);
                for (label, tip, cmd, p) in [
                    ("T", "New Text Layer", "graphics.newText", json!({"clip": clip.0, "text": "New Text"})),
                    ("", "New Rectangle", "graphics.newShape", json!({"clip": clip.0, "shape": "rectangle"})),
                    ("", "New Ellipse", "graphics.newShape", json!({"clip": clip.0, "shape": "ellipse", "size": [200, 200]})),
                ] {
                    let r = letter_button(ui, label, tip, false, &t, Tokens::semibold(13.0));
                    if label.is_empty() {
                        let ir = Rect::from_center_size(r.rect.center(), vec2(14.0, 14.0));
                        icons::paint(ui.painter(), ir, if tip == "New Rectangle" { Icon::Rectangle } else { Icon::Ellipse }, t.text_dim);
                    }
                    autos.push((format!("graphics.{}", tip.replace(' ', "")), r.rect, tip.into()));
                    if r.clicked() {
                        actions.push((cmd.into(), p));
                    }
                }
                ui.add_space(8.0);
                if let Some(l) = sel {
                    for (label, tip, to) in [("↑", "Bring Forward", "forward"), ("↓", "Send Backward", "backward")] {
                        let r = letter_button(ui, label, tip, false, &t, Tokens::ui(13.0));
                        autos.push((format!("graphics.arrange.{to}"), r.rect, tip.into()));
                        if r.clicked() {
                            actions.push(("graphics.arrangeLayer".into(), json!({"clip": clip.0, "layer": l, "to": to})));
                        }
                    }
                    let (dr, dresp) = ui.allocate_exact_size(vec2(24.0, 22.0), Sense::click());
                    icons::paint(ui.painter(), dr.shrink(4.0), Icon::Trash, if dresp.hovered() { t.text } else { t.text_dim });
                    autos.push(("graphics.deleteLayer".into(), dr, "Delete Layer".into()));
                    if dresp.on_hover_text("Delete Layer").clicked() {
                        actions.push(("graphics.deleteLayer".into(), json!({"clip": clip.0, "layer": l})));
                    }
                }
            });
            for (li, &ei) in idx.iter().enumerate().rev() {
                let e = &it.effects[ei];
                let (r, resp) = ui.allocate_exact_size(vec2(ui.available_width(), 26.0), Sense::click());
                let on = sel == Some(li);
                if on {
                    ui.painter().rect_filled(r, 2.0, Color32::from_rgb(0x2e, 0x3d, 0x55));
                } else if resp.hovered() {
                    ui.painter().rect_filled(r, 2.0, t.hover);
                }
                let kind = if e.effect == graphic::TEXT_LAYER { "T" } else { "◆" };
                ui.painter().text(pos2(r.min.x + 22.0, r.center().y), Align2::CENTER_CENTER, kind, Tokens::semibold(12.0), t.text_dim);
                ui.painter().text(pos2(r.min.x + 38.0, r.center().y), Align2::LEFT_CENTER, layer_display_name(e, li), Tokens::ui(12.0), t.text);
                // visibility
                let er = Rect::from_center_size(pos2(r.max.x - 14.0, r.center().y), vec2(16.0, 16.0));
                let eresp = ui.interact(er, egui::Id::new(("gfx-eye", clip.0, li)), Sense::click());
                icons::paint(ui.painter(), er.shrink(2.0), if e.enabled { Icon::Eye } else { Icon::EyeOff }, t.text_dim);
                autos.push((format!("graphics.layers.{li}.visible"), er, "Toggle visibility".into()));
                autos.push((format!("graphics.layers.{li}"), r, layer_display_name(e, li)));
                if eresp.clicked() {
                    actions.push(("graphics.set".into(), json!({"clip": clip.0, "layer": li, "props": {"enabled": !e.enabled}})));
                } else if resp.clicked() {
                    actions.push(("graphics.selectLayer".into(), json!({"clip": clip.0, "layers": [li]})));
                }
            }
        }
        // ---- template properties (a graphic made from a graphics template)
        if it.graphic.as_ref().is_some_and(|m| m.template.is_some()) && section(ui, app, "Template Properties", &t) {
            crate::panels::graphics_templates::template_controls(app, ui, clip, &it, mt, &mut autos, &mut actions);
        }
        let Some(l) = sel else {
            if section(ui, app, "Responsive Design - Time", &t) {
                crate::panels::graphics_templates::responsive_time(app, ui, clip, &it, &mut autos, &mut actions);
            }
            ui.add_space(12.0);
            ui.label(egui::RichText::new("Select a layer to edit its properties.").color(t.text_faint));
            return;
        };
        let e = &it.effects[idx[l]];
        // a Type-tool selection in this layer: character properties apply to the selected characters
        let range = app.ui.gfx_edit.as_ref().filter(|ed| ed.clip == clip.0 && ed.layer == l && ed.caret != ed.anchor).and_then(|ed| {
            let text = match pv(e, "text", mt) {
                ParamValue::Text(s) => s,
                _ => return None,
            };
            let ch = |b: usize| text.get(..b.min(text.len())).map_or(0, |s| s.chars().count());
            Some((ch(ed.caret.min(ed.anchor)), ch(ed.caret.max(ed.anchor))))
        });
        let mut cx = Ctx { clip, layer: l, e, mt, t, actions: Vec::new(), autos: Vec::new(), range };
        if section(ui, app, "Responsive Design - Position", &t) {
            crate::panels::graphics_templates::responsive_position(app, ui, clip, &it, l, &mut cx.autos, &mut cx.actions);
        }
        // ---- align and transform
        if section(ui, app, "Align and Transform", &t) {
            ui.horizontal(|ui| {
                ui.add_space(14.0);
                for (how, tip) in [
                    ("left", "Align Left"),
                    ("hcenter", "Align Center Horizontally"),
                    ("right", "Align Right"),
                    ("top", "Align Top"),
                    ("vcenter", "Align Center Vertically"),
                    ("bottom", "Align Bottom"),
                ] {
                    let r = glyph_button(ui, how, tip, false, &t);
                    cx.autos.push((format!("graphics.align.{how}"), r.rect, tip.into()));
                    if r.clicked() {
                        cx.actions.push(("graphics.align".into(), json!({"clip": clip.0, "align": how})));
                    }
                }
                ui.add_space(6.0);
                for (how, axis, tip) in [("dh", "horizontal", "Distribute Horizontally"), ("dv", "vertical", "Distribute Vertically")] {
                    let r = glyph_button(ui, how, tip, false, &t);
                    cx.autos.push((format!("graphics.distribute.{axis}"), r.rect, tip.into()));
                    if r.clicked() {
                        cx.actions.push(("graphics.distribute".into(), json!({"clip": clip.0, "axis": axis, "layers": (0..idx.len()).collect::<Vec<_>>()})));
                    }
                }
            });
            cx.point(ui, "Position", "position");
            cx.point(ui, "Anchor point", "anchor");
            cx.number(ui, "Scale", "scale", 0.5, (0.0, 100_000.0), 0, " %");
            cx.number(ui, "Rotation", "rotation", 0.5, (-36_000.0, 36_000.0), 1, " °");
            cx.number(ui, "Opacity", "opacity", 0.5, (0.0, 100.0), 0, " %");
        }
        // ---- text
        if e.effect == graphic::TEXT_LAYER && section(ui, app, "Text", &t) {
            // source text (multi-line), committed as it is typed
            let mut s = match pv(e, "text", mt) {
                ParamValue::Text(s) => s,
                _ => String::new(),
            };
            let before = s.clone();
            ui.horizontal(|ui| {
                ui.add_space(14.0);
                let r = ui.add(egui::TextEdit::multiline(&mut s).desired_rows(2).desired_width(ui.available_width() - 8.0).font(Tokens::ui(12.0)));
                cx.autos.push(("graphics.sourceText".into(), r.rect, "Source Text".into()));
            });
            if s != before {
                cx.actions.push(("graphics.setText".into(), json!({"clip": clip.0, "layer": l, "text": s, "merge": true})));
            }
            let family = match pv(e, "font", mt) {
                ParamValue::Text(f) => f,
                _ => String::new(),
            };
            let style = match pv(e, "font_style", mt) {
                ParamValue::Text(f) => f,
                _ => String::new(),
            };
            let fams = lumiere_codecbase_text::families();
            let (_, mut vui) = cx.row(ui, "Font");
            let r = egui::ComboBox::from_id_salt(("gfx-font", clip.0, l)).selected_text(&family).width(170.0).height(400.0).show_ui(&mut vui, |ui| {
                if !lumiere_codecbase_text::fonts::system_scanned() {
                    lumiere_codecbase_text::fonts::scan_system();
                }
                for (f, _) in &fams {
                    if ui.selectable_label(*f == family, f).clicked() {
                        cx.set("font", json!(f));
                    }
                }
            });
            cx.auto("font", r.response.rect, "Font");
            let styles: Vec<String> = fams.iter().find(|(f, _)| f.eq_ignore_ascii_case(&family)).map(|(_, s)| s.clone()).unwrap_or_default();
            let (_, mut vui) = cx.row(ui, "Font Style");
            let r = egui::ComboBox::from_id_salt(("gfx-style", clip.0, l)).selected_text(&style).width(170.0).show_ui(&mut vui, |ui| {
                for st in styles.iter().map(String::as_str).chain(["Bold", "Italic", "Bold Italic"]).filter({
                    let mut seen = Vec::new();
                    move |x: &&str| {
                        if seen.contains(x) {
                            false
                        } else {
                            seen.push(*x);
                            true
                        }
                    }
                }) {
                    if ui.selectable_label(st == style, st).clicked() {
                        cx.set("font_style", json!(st));
                    }
                }
            });
            cx.auto("font_style", r.response.rect, "Font Style");
            cx.number(ui, "Font Size", "size", 0.5, (1.0, 2000.0), 0, "");
            let align = match pv(e, "align", mt) {
                ParamValue::Choice(c) => c,
                _ => 0,
            };
            let (_, mut vui) = cx.row(ui, "Alignment");
            for (i, (how, tip)) in [("pl", "Left Align Text"), ("pc", "Center Align Text"), ("pr", "Right Align Text"), ("pj", "Justify")].iter().enumerate() {
                let r = glyph_button(&mut vui, how, tip, align == i as u32, &t);
                cx.auto(&format!("align.{i}"), r.rect, tip);
                if r.clicked() {
                    cx.set("align", json!(i));
                }
            }
            cx.number(ui, "Tracking", "tracking", 1.0, (-1000.0, 10_000.0), 0, "");
            cx.number(ui, "Leading", "leading", 0.5, (-5000.0, 5000.0), 0, "");
            cx.number(ui, "Baseline Shift", "baseline_shift", 0.5, (-5000.0, 5000.0), 0, "");
            cx.number(ui, "Text Box Width", "box_width", 2.0, (0.0, 100_000.0), 0, "");
            let (_, mut vui) = cx.row(ui, "Style");
            let flag = |id: &str| pv(e, id, mt).as_bool().unwrap_or(false);
            let caps = match pv(e, "caps", mt) {
                ParamValue::Choice(c) => c,
                _ => 0,
            };
            for (id, label, tip, on, font) in [
                ("faux_bold", "T", "Faux Bold", flag("faux_bold"), Tokens::semibold(14.0)),
                ("faux_italic", "T", "Faux Italic", flag("faux_italic"), egui::FontId::new(14.0, egui::FontFamily::Proportional)),
                ("all_caps", "TT", "All Caps", caps == 1, Tokens::ui(12.0)),
                ("small_caps", "Tt", "Small Caps", caps == 2, Tokens::ui(12.0)),
                ("underline", "U", "Underline", flag("underline"), Tokens::ui(13.0)),
            ] {
                let r = letter_button(&mut vui, label, tip, on, &t, font);
                if id == "faux_italic" {
                    // slanted stroke to read as italic
                    let c = r.rect.center();
                    vui.painter().line_segment([c + vec2(-5.0, 6.0), c + vec2(5.0, -6.0)], Stroke::new(0.8, t.text_faint));
                }
                cx.auto(id, r.rect, tip);
                if r.clicked() {
                    match id {
                        "all_caps" => cx.set("caps", json!(if caps == 1 { 0 } else { 1 })),
                        "small_caps" => cx.set("caps", json!(if caps == 2 { 0 } else { 2 })),
                        _ => cx.set(id, json!(!on)),
                    }
                }
            }
            let (_, mut vui) = cx.row(ui, "OpenType");
            cx.check(&mut vui, "kerning", "Kerning");
            cx.check(&mut vui, "ligatures", "Ligatures");
        }
        if e.effect == graphic::SHAPE_LAYER && section(ui, app, "Shape", &t) {
            let (_, mut vui) = cx.row(ui, "Shape");
            cx.choice(&mut vui, "shape", graphic::SHAPE_OPTS, 120.0);
            cx.point(ui, "Size", "size");
            cx.number(ui, "Corner Radius", "corner_radius", 0.5, (0.0, 10_000.0), 0, "");
            cx.number(ui, "Polygon Sides", "sides", 0.05, (3.0, 64.0), 0, "");
        }
        // ---- appearance
        if section(ui, app, "Appearance", &t) {
            let (_, mut vui) = cx.row(ui, "Fill");
            cx.check(&mut vui, "fill", "");
            cx.color(&mut vui, "fill_color");
            for (on, col, w, kind, label) in
                [("stroke", "stroke_color", "stroke_width", "stroke_type", "Stroke"), ("stroke2", "stroke2_color", "stroke2_width", "stroke2_type", "Stroke 2")]
            {
                let (_, mut vui) = cx.row(ui, label);
                cx.check(&mut vui, on, "");
                cx.color(&mut vui, col);
                vui.add_space(6.0);
                cx.number_in(&mut vui, w, 0.2, (0.0, 1000.0), 1, "");
                vui.add_space(6.0);
                cx.choice(&mut vui, kind, graphic::STROKE_OPTS, 70.0);
            }
            let (_, mut vui) = cx.row(ui, "Background");
            let bg = cx.check(&mut vui, "background", "");
            cx.color(&mut vui, "background_color");
            if bg {
                cx.number(ui, "   Opacity", "background_opacity", 0.5, (0.0, 100.0), 0, " %");
                cx.number(ui, "   Size", "background_size", 0.5, (0.0, 1000.0), 0, "");
                cx.number(ui, "   Corner Radius", "background_radius", 0.5, (0.0, 1000.0), 0, "");
            }
            let (_, mut vui) = cx.row(ui, "Shadow");
            let sh = cx.check(&mut vui, "shadow", "");
            cx.color(&mut vui, "shadow_color");
            if sh {
                cx.number(ui, "   Opacity", "shadow_opacity", 0.5, (0.0, 100.0), 0, " %");
                cx.number(ui, "   Angle", "shadow_angle", 0.5, (-36_000.0, 36_000.0), 0, " °");
                cx.number(ui, "   Distance", "shadow_distance", 0.5, (0.0, 1000.0), 0, "");
                cx.number(ui, "   Size", "shadow_size", 0.5, (0.0, 1000.0), 0, "");
                cx.number(ui, "   Blur", "shadow_blur", 0.5, (0.0, 1000.0), 0, "");
            }
        }
        actions.append(&mut cx.actions);
        autos.append(&mut cx.autos);
    });
    for (id, r, label) in autos {
        app.auto.add(&id, r, &label);
    }
    for (cmd, p) in actions {
        if let Err(e) = app.session.execute(&cmd, p) {
            app.ui.status = e.to_string();
        }
    }
}
