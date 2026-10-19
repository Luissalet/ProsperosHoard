//! The vertical Tools panel. Grouped tools show a small flyout triangle; right-click (or long
//! press) opens the group to pick another tool.

use egui::{Rect, Sense, pos2, vec2};

use crate::LumiereEditApp;
use crate::icons;
use crate::state::Tool;

pub fn show(app: &mut LumiereEditApp, ui: &mut egui::Ui, rect: Rect) {
    let t = app.tokens;
    let mut y = rect.min.y + 4.0;
    let size = 32.0;
    for group in Tool::groups() {
        let current = if group.contains(&app.ui.tool) { app.ui.tool } else { group[0] };
        let r = Rect::from_min_size(pos2(rect.center().x - size / 2.0, y), vec2(size, size));
        let resp = ui.interact(r, egui::Id::new(("tool", format!("{:?}", group[0]))), Sense::click()).on_hover_text(format!(
            "{} ({})",
            current.label(),
            current.shortcut()
        ));
        for tl in &group {
            app.auto.add(&format!("tools.{tl:?}"), r, tl.label());
        }
        let active = group.contains(&app.ui.tool);
        if active {
            ui.painter().rect_filled(Rect::from_center_size(r.center(), vec2(26.0, 26.0)), 4.0, t.accent);
        } else if resp.hovered() {
            ui.painter().rect_filled(r, 4.0, t.hover);
        }
        icons::paint(ui.painter(), Rect::from_center_size(r.center(), vec2(17.0, 17.0)), current.icon(), if active { egui::Color32::WHITE } else { t.icon });
        if group.len() > 1 {
            let c = r.right_bottom() - vec2(3.0, 3.0);
            ui.painter().add(egui::Shape::convex_polygon(vec![c, c - vec2(5.0, 0.0), c - vec2(0.0, 5.0)], t.text_dim, egui::Stroke::NONE));
        }
        if resp.clicked() {
            app.ui.tool = current;
        }
        if group.len() > 1 {
            egui::Popup::context_menu(&resp).show(|ui| {
                for tl in &group {
                    if ui.selectable_label(app.ui.tool == *tl, format!("{}   {}", tl.label(), tl.shortcut())).clicked() {
                        app.ui.tool = *tl;
                    }
                }
            });
        }
        y += size + 4.0;
        if y > rect.max.y - size {
            break;
        }
    }
}
