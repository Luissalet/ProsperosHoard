//! Effects panel: searchable folder tree of presets, audio/video effects and transitions (from the
//! effect definitions). Drag onto a clip or edit point; double-click applies to the selection.

use egui::{Align2, Color32, Rect, Sense, pos2, vec2};
use serde_json::json;

use crate::LumiereEditApp;
use crate::icons::{self, Icon};
use crate::theme::Tokens;

pub fn show(app: &mut LumiereEditApp, ui: &mut egui::Ui, rect: Rect) {
    let t = app.tokens;
    let sr = Rect::from_min_size(rect.min + vec2(8.0, 6.0), vec2(rect.width() - 16.0, 22.0));
    let mut child = ui.new_child(egui::UiBuilder::new().max_rect(sr));
    let mut q = app.ui.effects_search.clone();
    crate::widgets::search_field(&mut child, &mut q, "Search effects", sr.width(), &t);
    app.ui.effects_search = q.clone();
    let body = Rect::from_min_max(pos2(rect.min.x, sr.max.y + 6.0), rect.max);
    let mut bui = ui.new_child(egui::UiBuilder::new().max_rect(body).id_salt("fx-body"));
    bui.set_clip_rect(body);
    let filter = q.to_ascii_lowercase();
    let defs = lumiere_codecbase_project::effect_defs();
    // folder tree: top-level categories in Premiere's order
    let tops = lumiere_codecbase_project::vtransition::EFFECT_TOP_FOLDERS;
    let mut apply: Option<String> = None;
    let mut preset_action: Option<(String, String)> = None;
    egui::ScrollArea::vertical().id_salt("fx-scroll").auto_shrink([false, false]).show(&mut bui, |ui| {
        for &top in tops {
            if top == "Presets" {
                let any = filter.is_empty() || app.session.presets.all().iter().any(|p| p.name.to_ascii_lowercase().contains(&filter));
                if any && folder_row(app, ui, top, 0, &filter) {
                    preset_action = crate::panels::presets::folder_rows(app, ui, &filter).or(preset_action.take());
                }
                continue;
            }
            let mut items: Vec<_> = defs
                .iter()
                .filter(|d| d.category.first() == Some(&top))
                .filter(|d| filter.is_empty() || d.name.to_ascii_lowercase().contains(&filter))
                .collect();
            // Premiere lists effect folders and effects alphabetically (obsolete ones last)
            if top == "Video Effects" || top == "Legacy" {
                items.sort_by_key(|d| (d.category.get(1) == Some(&"Obsolete"), d.category.get(1).copied().unwrap_or(""), d.name.to_ascii_lowercase()));
            }
            if !filter.is_empty() && items.is_empty() {
                continue;
            }
            let open = folder_row(app, ui, top, 0, &filter);
            if !open {
                continue;
            }
            // Sub-folders in definition order; loose items (no sub-folder, e.g. Audio Effects ▸
            // Balance / Mute / Volume) come last, outside any folder.
            let sub = |d: &lumiere_codecbase_project::EffectDef| d.category.get(1).copied().unwrap_or("");
            let mut subs: Vec<&str> = items.iter().map(|d| sub(d)).collect();
            subs.dedup();
            subs.sort_by_key(|s| s.is_empty());
            let mut seen = Vec::new();
            for s in subs {
                if seen.contains(&s) {
                    continue;
                }
                seen.push(s);
                let key = format!("{top}/{s}");
                if !s.is_empty() && !folder_row(app, ui, &key, 1, &filter) {
                    continue;
                }
                for d in items.iter().filter(|d| sub(d) == s) {
                    let (r, resp) = ui.allocate_exact_size(vec2(ui.available_width(), 20.0), Sense::click_and_drag());
                    if resp.hovered() {
                        ui.painter().rect_filled(r, 0.0, t.hover);
                    }
                    let x = r.min.x + 46.0;
                    let icon_r = Rect::from_center_size(pos2(x - 10.0, r.center().y), vec2(13.0, 13.0));
                    let is_tr = matches!(d.kind, lumiere_codecbase_project::EffectKind::VideoTransition | lumiere_codecbase_project::EffectKind::AudioTransition);
                    ui.painter().rect_stroke(icon_r, 2.0, egui::Stroke::new(1.0, t.text_dim), egui::StrokeKind::Inside);
                    if is_tr {
                        ui.painter().line_segment([icon_r.left_bottom(), icon_r.right_top()], egui::Stroke::new(1.0, t.text_dim));
                    } else {
                        ui.painter().text(icon_r.center(), Align2::CENTER_CENTER, "fx", Tokens::ui(8.0), t.text_dim);
                    }
                    ui.painter().text(pos2(x, r.center().y), Align2::LEFT_CENTER, d.name, Tokens::ui(12.0), t.text);
                    // badges: accelerated / 32-bit / YUV
                    let mut bx = r.max.x - 8.0;
                    for (on, label) in [(d.yuv, "YUV"), (d.float32, "32"), (d.accelerated, "⚡")] {
                        if on {
                            let br = Rect::from_min_size(pos2(bx - 22.0, r.min.y + 3.0), vec2(20.0, 14.0));
                            ui.painter().rect_filled(br, 2.0, Color32::from_rgb(48, 48, 48));
                            ui.painter().text(br.center(), Align2::CENTER_CENTER, label, Tokens::ui(8.5), t.text_dim);
                            bx -= 24.0;
                        }
                    }
                    app.auto.add(&format!("effects.item.{}", d.id), r, d.name);
                    if resp.drag_started() {
                        crate::panels::start_drag_effect(ui, d.id);
                    }
                    if resp.double_clicked() {
                        apply = Some(d.id.to_string());
                    }
                }
            }
        }
    });
    if let Some((cmd, name)) = preset_action {
        crate::panels::presets::run(app, &cmd, &name);
    }
    if let Some(id) = apply {
        let r = app.session.execute("effects.apply", json!({"effect": id}));
        if let Err(e) = r {
            app.ui.status = e.to_string();
        }
    }
}

fn folder_row(app: &mut LumiereEditApp, ui: &mut egui::Ui, key: &str, depth: usize, filter: &str) -> bool {
    let t = app.tokens;
    let open = !filter.is_empty() || app.ui.expanded_fx.iter().any(|k| k == key);
    let (r, resp) = ui.allocate_exact_size(vec2(ui.available_width(), 20.0), Sense::click());
    if resp.hovered() {
        ui.painter().rect_filled(r, 0.0, t.hover);
    }
    let x = r.min.x + 8.0 + depth as f32 * 16.0;
    icons::paint(
        ui.painter(),
        Rect::from_center_size(pos2(x + 4.0, r.center().y), vec2(10.0, 10.0)),
        if open { Icon::ChevronDown } else { Icon::ChevronRight },
        t.text_dim,
    );
    icons::paint(ui.painter(), Rect::from_center_size(pos2(x + 18.0, r.center().y), vec2(13.0, 13.0)), Icon::Folder, t.text_dim);
    let name = key.rsplit('/').next().unwrap_or(key);
    ui.painter().text(pos2(x + 30.0, r.center().y), Align2::LEFT_CENTER, name, Tokens::ui(12.0), t.text);
    app.auto.add(&format!("effects.folder.{key}"), r, name);
    if resp.clicked() {
        if open {
            app.ui.expanded_fx.retain(|k| k != key);
        } else {
            app.ui.expanded_fx.push(key.to_string());
        }
    }
    open
}
