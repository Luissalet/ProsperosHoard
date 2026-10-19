//! Headless checks for the M13.6 panels: each opens from the Window menu as a real panel and
//! registers its automation ids; the Footage panel's buttons edit into the comp; the Progress
//! panel lists and cancels a job; Lumetri Scopes switch scope through their dropdown.

use lumiere_motion_engine::Session;
use lumiere_motion_ui_egui::LumiereMotionApp;
use lumiere_motion_ui_egui::dock::PanelKind;
use egui::{Event, Pos2, pos2};
use egui_kittest::Harness;
use serde_json::json;

fn harness() -> Harness<'static, LumiereMotionApp> {
    let mut s = Session::default();
    s.execute("comp.new", json!({"name": "Panels", "width": 320, "height": 180, "duration": 4})).unwrap();
    s.execute("layer.newSolid", json!({"name": "Plate", "color": "#406080"})).unwrap();
    let mut h = Harness::builder().with_size(egui::vec2(1600.0, 1000.0)).build_eframe(|_| LumiereMotionApp::new(s));
    h.run_steps(3);
    h
}

fn open(h: &mut Harness<'_, LumiereMotionApp>, panel: &str) {
    let ctx = h.ctx.clone();
    lumiere_motion_ui_egui::menus::invoke(h.state_mut(), &ctx, "window.panel", json!({"panel": panel})).unwrap();
    h.run_steps(3);
}

fn click(h: &mut Harness<'_, LumiereMotionApp>, id: &str) {
    let e = h.state().auto.find(id).unwrap_or_else(|| panic!("no {id}")).clone();
    let p: Pos2 = pos2(e.rect[0] + e.rect[2] / 2.0, e.rect[1] + e.rect[3] / 2.0);
    h.input_mut().events.push(Event::PointerMoved(p));
    h.input_mut().events.push(Event::PointerButton { pos: p, button: egui::PointerButton::Primary, pressed: true, modifiers: Default::default() });
    h.step();
    h.input_mut().events.push(Event::PointerButton { pos: p, button: egui::PointerButton::Primary, pressed: false, modifiers: Default::default() });
    h.run_steps(2);
}

#[test]
fn window_menu_opens_the_new_panels() {
    let mut h = harness();
    for (name, kind, auto) in [
        ("lumetriScopes", PanelKind::LumetriScopes, "scopes.kind"),
        ("footage", PanelKind::Footage, "footage.empty"),
        ("mediaBrowser", PanelKind::MediaBrowser, "mediaBrowser.path"),
        ("metadata", PanelKind::Metadata, "metadata.projectComment"),
        ("progress", PanelKind::Progress, ""),
        ("contentAwareFill", PanelKind::ContentAwareFill, "contentFill.method"),
    ] {
        open(&mut h, name);
        assert!(h.state().ui.dock.contains(kind) || h.state().ui.floating.iter().any(|f| f.panels.contains(&kind)), "{name} shown");
        if !auto.is_empty() {
            assert!(h.state().auto.find(auto).is_some(), "{name}: {auto}");
        }
    }
}

#[test]
fn scopes_switch_kind() {
    let mut h = harness();
    open(&mut h, "lumetriScopes");
    assert!(h.state().auto.find("scopes.plot").is_some());
    h.state_mut().ui.scopes.scope = "vectorscopeYuv".into();
    h.run_steps(2);
    assert_eq!(h.state().auto.find("scopes.plot").unwrap().label, "Vectorscope YUV");
    h.state_mut().ui.scopes.scope = "histogram".into();
    h.run_steps(2);
    assert_eq!(h.state().auto.find("scopes.kind").unwrap().label, "Histogram");
}

#[test]
fn footage_panel_buttons_edit_into_the_comp() {
    let mut h = harness();
    let item = h.state().session.project.items.values().find(|i| matches!(i.kind, lumiere_motion_engine::project::ItemKind::Solid(_))).unwrap().id.0;
    h.state_mut().session.execute("footage.open", json!({"item": item})).unwrap();
    h.run_steps(4);
    assert!(h.state().ui.dock.contains(PanelKind::Footage) || h.state().ui.floating.iter().any(|f| f.panels.contains(&PanelKind::Footage)));
    assert!(h.state().auto.find("footage.overlayEdit").is_some());
    let before = h.state().session.active_comp().unwrap().layers.len();
    click(&mut h, "footage.overlayEdit");
    assert_eq!(h.state().session.active_comp().unwrap().layers.len(), before + 1);
    click(&mut h, "footage.rippleInsertEdit");
    assert!(h.state().session.active_comp().unwrap().layers.len() >= before + 2);
}

#[test]
fn progress_panel_cancels_a_job() {
    let mut h = harness();
    h.state_mut()
        .session
        .spawn_task("test", "Long analysis", false, |ctl| {
            while ctl.progress(1, 2) {
                std::thread::sleep(std::time::Duration::from_millis(2));
            }
            Err("cancelled".into())
        })
        .unwrap();
    open(&mut h, "progress");
    let id = h.state().session.jobs()[0].id.clone();
    assert!(h.state().auto.find(&format!("progress.job.{id}")).is_some());
    click(&mut h, &format!("progress.cancel.{id}"));
    h.state_mut().session.wait_jobs();
    h.run_steps(2);
    assert!(h.state().session.jobs().is_empty());
    assert_eq!(h.state().session.job_log.last().unwrap().status, "cancelled");
}

/// Headless look at the panels (wgpu offscreen; needs a GPU adapter). Run with
/// `PANELS_SNAPSHOT=/abs/dir cargo test -p lumiere-motion-ui-egui --test ui_panels -- --ignored`.
#[test]
#[ignore]
fn panels_snapshot() {
    let mut s = Session::default();
    s.execute("file.openDemoProject", json!({})).unwrap();
    let solid = s.project.items.values().find(|i| matches!(i.kind, lumiere_motion_engine::project::ItemKind::Solid(_))).map(|i| i.id.0);
    let mut app = Some(LumiereMotionApp::new(s));
    let mut h = Harness::builder().with_size(egui::vec2(1600.0, 1000.0)).build_eframe(|_| app.take().expect("app"));
    h.run_steps(3);
    let ctx = h.ctx.clone();
    lumiere_motion_ui_egui::menus::invoke(h.state_mut(), &ctx, "window.workspace", json!({"name": "All Panels"})).unwrap();
    h.run_steps(3);
    let dir = std::env::var("PANELS_SNAPSHOT").unwrap_or_else(|_| concat!(env!("CARGO_MANIFEST_DIR"), "/../../target/test-out").into());
    std::fs::create_dir_all(&dir).unwrap();
    if let Some(id) = solid {
        h.state_mut().session.execute("footage.open", json!({"item": id})).unwrap();
    }
    for (panel, scope) in [
        ("lumetriScopes", "waveformRgb"),
        ("lumetriScopes", "vectorscopeYuv"),
        ("lumetriScopes", "histogram"),
        ("lumetriScopes", "paradeRgb"),
        ("contentAwareFill", ""),
        ("metadata", ""),
        ("progress", ""),
        ("mediaBrowser", ""),
        ("footage", ""),
    ] {
        if !scope.is_empty() {
            h.state_mut().ui.scopes.scope = scope.into();
        }
        open(&mut h, panel);
        h.run_steps(3);
        let img = h.render().expect("render");
        img.save(format!("{dir}/{panel}{scope}.png")).unwrap();
    }
}
