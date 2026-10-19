//! The Tools bar: home, the tool slots, tool options, snapping, workspaces and the community
//! buttons (Discord is always one click away).

use egui::{Align2, Color32, Rect, Sense, Stroke, pos2, vec2};
use serde_json::json;

use crate::icons::{self, Icon};
use crate::state::Tool;
use crate::theme::Tokens;
use crate::widgets;
use crate::{Dialog, LumiereMotionApp};

pub fn show(app: &mut LumiereMotionApp, ui: &mut egui::Ui, rect: Rect) {
    let t = app.tokens;
    let p = ui.painter().clone();
    p.rect_filled(rect, 0.0, t.header_bg);
    p.line_segment([rect.left_bottom(), rect.right_bottom()], Stroke::new(1.0, t.app_bg));
    let cy = rect.center().y;
    let mut x = rect.min.x + if app.integrated_titlebar && !app.ui.show_menu_bar { 80.0 } else { 10.0 };

    // Brand mark.
    let brand = Rect::from_min_size(pos2(x, cy - 12.0), vec2(24.0, 24.0));
    paint_logo(&p, brand);
    let bresp = ui.interact(brand, egui::Id::new("brand"), Sense::click());
    app.auto.add("header.about", brand, "About Lumiere Motion");
    if bresp.on_hover_text("About Lumiere Motion").clicked() {
        app.dialog = Some(Dialog::About);
    }
    x += 32.0;

    // Home.
    let home = Rect::from_min_size(pos2(x, cy - 13.0), vec2(26.0, 26.0));
    if widgets::icon_button(ui, home, Icon::Home, app.ui.start_screen, &t, egui::Id::new("tool-home")).on_hover_text("Home").clicked() {
        app.ui.start_screen = !app.ui.start_screen;
    }
    app.auto.add("header.home", home, "Home");
    x += 30.0;
    p.line_segment([pos2(x, cy - 10.0), pos2(x, cy + 10.0)], Stroke::new(1.0, t.separator));
    x += 6.0;

    // Tool slots (with group separators after camera tools and pan-behind).
    for (si, slot) in Tool::SLOTS.iter().enumerate() {
        let cur = app.ui.slot_tools.get(si).copied().unwrap_or(slot[0]);
        let r = Rect::from_min_size(pos2(x, cy - 13.0), vec2(26.0, 26.0));
        let active = slot.contains(&app.ui.tool);
        let id = egui::Id::new(("tool-slot", si));
        let resp = widgets::icon_button(ui, r, cur.icon(), active, &t, id);
        if slot.len() > 1 {
            // Flyout corner triangle.
            p.add(egui::Shape::convex_polygon(
                vec![pos2(r.max.x - 2.0, r.max.y - 6.0), pos2(r.max.x - 2.0, r.max.y - 2.0), pos2(r.max.x - 6.0, r.max.y - 2.0)],
                if active { Color32::WHITE } else { t.text_dim },
                Stroke::NONE,
            ));
        }
        app.auto.add(&format!("tools.{cur:?}"), r, cur.label());
        let tip = match cur.shortcut() {
            Some(s) => format!("{} ({})", cur.label(), crate::menus::shortcut_text(s)),
            None => cur.label().to_string(),
        };
        let resp = resp.on_hover_text(tip);
        if resp.clicked() {
            app.ui.tool = cur;
        }
        if slot.len() > 1 {
            resp.context_menu(|ui| {
                for tool in slot.iter() {
                    if ui.selectable_label(app.ui.tool == *tool, tool.label()).clicked() {
                        app.ui.tool = *tool;
                        app.ui.slot_tools[si] = *tool;
                        ui.close();
                    }
                }
            });
        }
        x += 28.0;
        if matches!(si, 2 | 5 | 7 | 10 | 13) {
            x += 4.0;
            p.line_segment([pos2(x, cy - 10.0), pos2(x, cy + 10.0)], Stroke::new(1.0, t.separator));
            x += 6.0;
        }
    }

    // Tool options.
    x += 10.0;
    if app.ui.tool.is_shape() || app.ui.tool == Tool::Pen {
        p.text(pos2(x, cy), Align2::LEFT_CENTER, "Fill:", Tokens::ui(12.0), t.text_dim);
        x += 28.0;
        let fr = Rect::from_center_size(pos2(x + 10.0, cy), vec2(20.0, 16.0));
        let c = app.ui.fill_color;
        if widgets::swatch(ui, fr, [c[0], c[1], c[2], 1.0], egui::Id::new("tool-fill"), &t).clicked() {
            widgets::open_popup(ui, egui::Id::new("tool-fill-pop"));
        }
        color_popup(ui, egui::Id::new("tool-fill-pop"), fr.left_bottom(), &mut app.ui.fill_color);
        x += 32.0;
        p.text(pos2(x, cy), Align2::LEFT_CENTER, "Stroke:", Tokens::ui(12.0), t.text_dim);
        x += 44.0;
        let sr = Rect::from_center_size(pos2(x + 10.0, cy), vec2(20.0, 16.0));
        let c = app.ui.stroke_color;
        if widgets::swatch(ui, sr, [c[0], c[1], c[2], 1.0], egui::Id::new("tool-stroke"), &t).clicked() {
            widgets::open_popup(ui, egui::Id::new("tool-stroke-pop"));
        }
        color_popup(ui, egui::Id::new("tool-stroke-pop"), sr.left_bottom(), &mut app.ui.stroke_color);
        x += 26.0;
        let (r, v, _) =
            widgets::hot_number_at(ui, pos2(x, cy - 9.0), egui::Id::new("tool-stroke-w"), app.ui.stroke_width as f64, 0.2, (0.0, 1000.0), 0, " px", &t);
        if let Some(v) = v {
            app.ui.stroke_width = v as f32;
        }
        x = r.max.x + 14.0;
    }
    if app.ui.tool.puppet_kind().is_some() {
        x = puppet_options(app, ui, &p, x, cy);
    }
    let snap = Rect::from_min_size(pos2(x, cy - 10.0), vec2(20.0, 20.0));
    // The engine owns snapping (View ▸ Snapping); the checkbox mirrors it.
    app.ui.snapping = app.session.state.snapping;
    if widgets::checkbox(ui, snap, app.ui.snapping, &t, egui::Id::new("snapping")).clicked() {
        let _ = app.session.execute("view.snapping", serde_json::json!({}));
        app.ui.snapping = app.session.state.snapping;
    }
    app.auto.add("header.snapping", snap, "Snapping");
    p.text(pos2(snap.max.x + 4.0, cy), Align2::LEFT_CENTER, "Snapping", Tokens::ui(12.0), t.text_dim);

    // Right side: community buttons, workspaces.
    let mut rx = rect.max.x - 10.0;
    let discord = Rect::from_min_max(pos2(rx - 104.0, cy - 13.0), pos2(rx, cy + 13.0));
    let dresp = ui.interact(discord, egui::Id::new("hdr-discord"), Sense::click());
    let dc = Color32::from_rgb(0x58, 0x65, 0xf2);
    p.rect_filled(discord, 13.0, if dresp.hovered() { dc.gamma_multiply(1.2) } else { dc });
    icons::paint(&p, Rect::from_center_size(pos2(discord.min.x + 16.0, cy), vec2(14.0, 14.0)), Icon::Chat, Color32::WHITE);
    p.text(pos2(discord.min.x + 28.0, cy), Align2::LEFT_CENTER, "Discord", Tokens::semibold(12.0), Color32::WHITE);
    app.auto.add("header.discord", discord, "Join the ArtCraft Discord");
    if dresp.on_hover_text("Join the ArtCraft community on Discord").clicked() {
        let _ = app.session.execute("help.discord", json!({}));
    }
    rx = discord.min.x - 6.0;
    for (id, icon, tip, cmd) in
        [("hdr-github", Icon::Code, "Lumiere Motion on GitHub", "help.github"), ("hdr-web", Icon::Globe, "Lumiere Motion on getartcraft.com", "help.appPage")]
    {
        let r = Rect::from_min_max(pos2(rx - 26.0, cy - 13.0), pos2(rx, cy + 13.0));
        if widgets::icon_button(ui, r, icon, false, &t, egui::Id::new(id)).on_hover_text(tip).clicked() {
            let _ = app.session.execute(cmd, json!({}));
        }
        app.auto.add(&format!("header.{}", &id[4..]), r, tip);
        rx = r.min.x - 4.0;
    }
    rx -= 10.0;
    p.line_segment([pos2(rx, cy - 10.0), pos2(rx, cy + 10.0)], Stroke::new(1.0, t.separator));
    rx -= 10.0;
    // Workspace tabs (right to left), with a » menu of all.
    let more = Rect::from_min_max(pos2(rx - 20.0, cy - 11.0), pos2(rx, cy + 11.0));
    let mresp = ui.interact(more, egui::Id::new("ws-more"), Sense::click());
    p.text(more.center(), Align2::CENTER_CENTER, "»", Tokens::ui(15.0), if mresp.hovered() { t.text } else { t.text_dim });
    app.auto.add("header.workspaces", more, "Workspaces");
    mresp.context_menu(|ui| ws_menu(app, ui));
    if mresp.clicked() {
        widgets::open_popup(ui, egui::Id::new("ws-pop"));
    }
    let names: Vec<String> = crate::dock::WORKSPACES.iter().map(|s| s.to_string()).collect();
    if let Some(i) = widgets::popup_menu(
        ui,
        egui::Id::new("ws-pop"),
        more.left_bottom() - vec2(140.0, 0.0),
        &names,
        crate::dock::WORKSPACES.iter().position(|w| *w == app.ui.workspace),
    ) {
        app.set_workspace(crate::dock::WORKSPACES[i]);
    }
    rx = more.min.x - 6.0;
    // After Effects 2026's workspace bar order (right to left here): Default, Review, Learn,
    // Small Screen, Standard.
    let shown = ["Standard", "Small Screen", "Learn", "Review", "Default"];
    for name in shown {
        let g = p.layout_no_wrap(name.to_string(), Tokens::ui(12.0), t.text);
        let w = g.size().x + 16.0;
        let r = Rect::from_min_max(pos2(rx - w, cy - 13.0), pos2(rx, cy + 13.0));
        if r.min.x < x + 120.0 {
            break;
        }
        let active = app.ui.workspace == name;
        let resp = ui.interact(r, egui::Id::new(("ws", name)), Sense::click());
        let col = if active {
            t.hot_text
        } else if resp.hovered() {
            t.text
        } else {
            t.text_dim
        };
        p.galley_with_override_text_color(pos2(r.min.x + 8.0, cy - g.size().y / 2.0), g, col);
        if active {
            p.line_segment([pos2(r.min.x + 8.0, r.max.y - 3.0), pos2(r.max.x - 8.0, r.max.y - 3.0)], Stroke::new(2.0, t.hot_text));
        }
        app.auto.add(&format!("header.workspace.{name}"), r, name);
        if resp.clicked() {
            app.set_workspace(name);
        }
        rx = r.min.x - 2.0;
    }
}

/// Puppet tool options: Mesh: Show, Expansion, Density (for new meshes and the selected
/// layer's meshes). Returns the next x.
fn puppet_options(app: &mut LumiereMotionApp, ui: &mut egui::Ui, p: &egui::Painter, mut x: f32, cy: f32) -> f32 {
    let t = app.tokens;
    let o = app.session.state.puppet.clone();
    p.text(pos2(x, cy), Align2::LEFT_CENTER, "Mesh:", Tokens::ui(12.0), t.text_dim);
    x += 40.0;
    let show = Rect::from_min_size(pos2(x, cy - 10.0), vec2(20.0, 20.0));
    if widgets::checkbox(ui, show, o.show_mesh, &t, egui::Id::new("puppet-show-mesh")).clicked() {
        let _ = app.session.execute("puppet.mesh", json!({"showMesh": !o.show_mesh}));
    }
    app.auto.add("header.puppet.showMesh", show, "Show mesh");
    p.text(pos2(show.max.x + 2.0, cy), Align2::LEFT_CENTER, "Show", Tokens::ui(12.0), t.text_dim);
    x = show.max.x + 44.0;
    for (label, key, v, range) in [("Expansion:", "expansion", o.expansion, (-100.0, 200.0)), ("Density:", "density", o.density, (0.0, 100.0))] {
        p.text(pos2(x, cy), Align2::LEFT_CENTER, label, Tokens::ui(12.0), t.text_dim);
        x += if key == "expansion" { 64.0 } else { 52.0 };
        let (r, nv, _) = widgets::hot_number_at(ui, pos2(x, cy - 9.0), egui::Id::new(("puppet-opt", key)), v, 0.2, range, 0, "", &t);
        app.auto.add(&format!("header.puppet.{key}"), r, label);
        if let Some(nv) = nv {
            let _ = app.session.execute("puppet.mesh", json!({key: nv, "merge": format!("puppet-opt-{key}")}));
        }
        x = r.max.x + 12.0;
    }
    // Record Options… (⌘/Ctrl-drag a pin records its motion in real time).
    let label = "Record Options...";
    let w = 104.0;
    let r = Rect::from_min_size(pos2(x, cy - 10.0), vec2(w, 20.0));
    let resp = ui.interact(r, egui::Id::new("puppet-record-options"), egui::Sense::click()).on_hover_text("⌘/Ctrl-drag a pin to record its motion");
    p.rect_stroke(r, 3.0, egui::Stroke::new(1.0, if resp.hovered() { t.accent } else { t.field_border }), egui::StrokeKind::Inside);
    p.text(r.center(), Align2::CENTER_CENTER, label, Tokens::ui(11.5), t.text);
    app.auto.add("header.puppet.recordOptions", r, label);
    if resp.clicked() {
        let ctx = ui.ctx().clone();
        if let Err(e) = crate::menus::invoke(app, &ctx, "puppet.recordOptions", json!({})) {
            app.ui.status = e;
        }
    }
    // Follow-Through… (select the leader pin, then Shift-click the pins that trail it).
    let label = "Follow-Through...";
    let r = Rect::from_min_size(pos2(r.max.x + 8.0, cy - 10.0), vec2(w, 20.0));
    let resp = ui
        .interact(r, egui::Id::new("puppet-follow"), egui::Sense::click())
        .on_hover_text("Select the leader pin, then Shift-click the pins that should trail it (hair, cloth, tails)");
    p.rect_stroke(r, 3.0, egui::Stroke::new(1.0, if resp.hovered() { t.accent } else { t.field_border }), egui::StrokeKind::Inside);
    p.text(r.center(), Align2::CENTER_CENTER, label, Tokens::ui(11.5), t.text);
    app.auto.add("header.puppet.follow", r, label);
    if resp.clicked() {
        let ctx = ui.ctx().clone();
        if let Err(e) = crate::menus::invoke(app, &ctx, "puppet.follow", json!({})) {
            app.ui.status = e;
        }
    }
    r.max.x + 16.0
}

fn ws_menu(app: &mut LumiereMotionApp, ui: &mut egui::Ui) {
    for w in app.workspace_names() {
        if ui.selectable_label(app.ui.workspace == w, &w).clicked() {
            app.set_workspace(&w);
            ui.close();
        }
    }
    ui.separator();
    if ui.button("Reset to Saved Layout").clicked() {
        let n = app.ui.workspace.clone();
        app.set_workspace(&n);
        ui.close();
    }
}

/// A small colour picker popup bound to an RGB value.
pub fn color_popup(ui: &mut egui::Ui, id: egui::Id, pos: egui::Pos2, c: &mut [f32; 3]) -> bool {
    let open_id = id.with("open");
    if !ui.data(|d| d.get_temp::<bool>(open_id).unwrap_or(false)) {
        return false;
    }
    let mut changed = false;
    let area = egui::Area::new(id.with("area")).order(egui::Order::Foreground).fixed_pos(pos + vec2(0.0, 4.0)).show(ui.ctx(), |ui| {
        egui::Frame::popup(ui.style()).show(ui, |ui| {
            let mut col =
                egui::Color32::from_rgb((c[0].clamp(0.0, 1.0) * 255.0) as u8, (c[1].clamp(0.0, 1.0) * 255.0) as u8, (c[2].clamp(0.0, 1.0) * 255.0) as u8);
            if egui::color_picker::color_picker_color32(ui, &mut col, egui::color_picker::Alpha::Opaque) {
                *c = [col.r() as f32 / 255.0, col.g() as f32 / 255.0, col.b() as f32 / 255.0];
                changed = true;
            }
        });
    });
    if ui.input(|i| i.pointer.any_pressed()) && !area.response.contains_pointer() || ui.input(|i| i.key_pressed(egui::Key::Escape)) {
        ui.data_mut(|d| d.insert_temp(open_id, false));
    }
    changed
}

/// The ArtCraft mark as supplied in `docs/brand` (the README's and getartcraft.com's logo; used
/// unmodified, see `docs/brand/LICENSE-brand.txt`).
static ARTCRAFT_MARK: &[u8] = include_bytes!("../../../docs/brand/artcraft-mark.png");

/// The ArtCraft mark in `r` (decoded once into a mipmapped texture, so it stays crisp from the
/// 24 px Tools bar to the About dialog). Nothing is drawn if it can't be decoded.
pub fn paint_logo(p: &egui::Painter, r: Rect) {
    let ctx = p.ctx();
    let id = egui::Id::new("artcraft-mark");
    let tex = ctx.data(|d| d.get_temp::<Option<egui::TextureHandle>>(id)).unwrap_or_else(|| {
        let tex = image::load_from_memory_with_format(ARTCRAFT_MARK, image::ImageFormat::Png).ok().map(|img| {
            let img = img.to_rgba8();
            let ci = egui::ColorImage::from_rgba_unmultiplied([img.width() as usize, img.height() as usize], img.as_raw());
            ctx.load_texture("artcraft-mark", ci, egui::TextureOptions::LINEAR.with_mipmap_mode(Some(egui::TextureFilter::Linear)))
        });
        ctx.data_mut(|d| d.insert_temp(id, tex.clone()));
        tex
    });
    if let Some(t) = tex {
        p.image(t.id(), r, Rect::from_min_max(pos2(0.0, 0.0), pos2(1.0, 1.0)), Color32::WHITE);
    }
}
