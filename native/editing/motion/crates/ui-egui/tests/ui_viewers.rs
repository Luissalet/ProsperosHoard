//! View ▸ New Viewer: several Composition viewers. New Viewer locks the viewer in use; opening a
//! comp goes to an unlocked viewer (a new one when every viewer is locked); a click on another
//! viewer makes it the active one; closing a viewer forgets it.

use lumiere_motion_engine::Session;
use lumiere_motion_engine::project::ItemId;
use lumiere_motion_ui_egui::LumiereMotionApp;
use lumiere_motion_ui_egui::dock::{PanelKind, Zone};
use lumiere_motion_ui_egui::panels::viewers;
use egui::{Event, pos2};
use egui_kittest::Harness;
use serde_json::{Value, json};

fn invoke(h: &mut Harness<'_, LumiereMotionApp>, id: &str, p: Value) {
    let ctx = h.ctx.clone();
    lumiere_motion_ui_egui::menus::invoke(h.state_mut(), &ctx, id, p).unwrap();
    h.run_steps(3);
}

fn click(h: &mut Harness<'_, LumiereMotionApp>, id: &str) {
    let e = h.state().auto.find(id).unwrap_or_else(|| panic!("no {id}")).clone();
    let p = pos2(e.rect[0] + e.rect[2] / 2.0, e.rect[1] + e.rect[3] / 2.0);
    h.input_mut().events.push(Event::PointerMoved(p));
    h.step();
    h.input_mut().events.push(Event::PointerButton { pos: p, button: egui::PointerButton::Primary, pressed: true, modifiers: Default::default() });
    h.input_mut().events.push(Event::PointerButton { pos: p, button: egui::PointerButton::Primary, pressed: false, modifiers: Default::default() });
    h.run_steps(3);
}

fn shows(h: &Harness<'_, LumiereMotionApp>, viewer: u32) -> Option<u64> {
    viewers::comp_of(h.state(), viewer).map(|c| c.0)
}

#[test]
fn new_viewers_lock_route_comps_and_activate_on_click() {
    let mut s = Session::default();
    let new =
        |s: &mut Session, name: &str| s.execute("comp.new", json!({"name": name, "width": 64, "height": 36, "duration": 1})).unwrap()["comp"].as_u64().unwrap();
    let c = new(&mut s, "C");
    let b = new(&mut s, "B");
    let a = new(&mut s, "A");
    let mut h = Harness::builder().with_size(egui::vec2(1600.0, 1000.0)).build_eframe(|_| LumiereMotionApp::new(s));
    h.run_steps(3);
    // View ▸ New Viewer: viewer 1 shows A too, viewer 0 (the Composition panel) is locked.
    invoke(&mut h, "view.newViewer", json!({}));
    assert!(h.state().ui.dock.contains(PanelKind::Viewer(1)));
    assert!(viewers::locked(h.state(), 0) && !viewers::locked(h.state(), 1));
    assert_eq!((h.state().ui.active_viewer, shows(&h, 0), shows(&h, 1)), (1, Some(a), Some(a)));
    // Side by side, so both draw.
    assert!(h.state_mut().edit_layout(|l| l.dock(PanelKind::Viewer(1), PanelKind::Composition, Zone::Right)));
    h.state_mut().ui.dock.activate(PanelKind::Composition);
    h.run_steps(3);
    // Opening B shows it in the active (unlocked) viewer; the locked one keeps A.
    invoke(&mut h, "comp.open", json!({"comp": b}));
    assert_eq!((h.state().ui.active_viewer, shows(&h, 0), shows(&h, 1)), (1, Some(a), Some(b)));
    assert!(h.state().auto.find("viewers.0").is_some(), "the other viewer draws passively");
    // A click on viewer 0 makes it active, and A the active comp.
    click(&mut h, "viewers.0");
    assert_eq!(h.state().ui.active_viewer, 0);
    assert_eq!(h.state().session.active_comp_id(), Some(ItemId(a)));
    assert_eq!(shows(&h, 1), Some(b));
    // Viewer 0 is locked to A: opening C goes to the unlocked viewer 1.
    invoke(&mut h, "comp.open", json!({"comp": c}));
    assert_eq!((h.state().ui.active_viewer, shows(&h, 0), shows(&h, 1)), (1, Some(a), Some(c)));
    // Every viewer locked: opening B makes a new viewer.
    h.state_mut().ui.locked_tabs.insert(PanelKind::Viewer(1).id());
    invoke(&mut h, "comp.open", json!({"comp": b}));
    assert_eq!(h.state().ui.active_viewer, 2);
    assert!(h.state().ui.dock.contains(PanelKind::Viewer(2)));
    assert_eq!((shows(&h, 0), shows(&h, 1), shows(&h, 2)), (Some(a), Some(c), Some(b)));
    // Closing the active viewer forgets it; another open viewer takes over.
    h.state_mut().close_panel(PanelKind::Viewer(2));
    h.run_steps(2);
    assert!(!h.state().ui.viewers.contains_key(&2));
    // Viewer 0 takes over with its own comp (it is locked to A), which becomes the active comp.
    assert_eq!(h.state().ui.active_viewer, 0);
    assert_eq!((shows(&h, 0), h.state().session.active_comp_id()), (Some(a), Some(ItemId(a))));
    // The tab names the comp each viewer shows.
    let titles: Vec<String> = h
        .state()
        .auto
        .elements
        .iter()
        .filter(|e| e.id.starts_with("panel.tab.Composition") || e.id.starts_with("panel.tab.Viewer"))
        .map(|e| e.label.clone())
        .collect();
    assert!(titles.iter().any(|t| t == "Composition A") && titles.iter().any(|t| t == "Composition C"), "{titles:?}");
}

/// `cargo test -p lumiere-motion-ui-egui --test ui_viewers -- --ignored`: two viewers side by side
/// (`target/test-out/viewers.png`).
#[test]
#[ignore]
fn viewers_snapshot() {
    let mut s = Session::default();
    s.execute("file.openDemoProject", json!({})).unwrap();
    let other = s.project.items.values().filter(|i| i.as_comp().is_some()).map(|i| i.id).find(|id| Some(*id) != s.active_comp_id()).unwrap();
    let mut h = Harness::builder().with_size(egui::vec2(1600.0, 1000.0)).build_eframe(|_| LumiereMotionApp::new(s));
    h.run_steps(3);
    invoke(&mut h, "view.newViewer", json!({}));
    assert!(h.state_mut().edit_layout(|l| l.dock(PanelKind::Viewer(1), PanelKind::Composition, Zone::Right)));
    h.state_mut().ui.dock.activate(PanelKind::Composition);
    invoke(&mut h, "comp.open", json!({"comp": other.0}));
    for _ in 0..300 {
        h.step();
        if h.state().frames.inflight() == 0 {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(20));
    }
    h.run_steps(4);
    let dir = concat!(env!("CARGO_MANIFEST_DIR"), "/../../target/test-out");
    std::fs::create_dir_all(dir).unwrap();
    h.render().unwrap().save(format!("{dir}/viewers.png")).unwrap();
}
