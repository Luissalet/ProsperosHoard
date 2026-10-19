//! Headless Effect Controls checks (egui_kittest): the AE-style widgets register their
//! automation ids, the on-viewer effect point control appears for the selected effect, and the
//! crosshair / eyedropper picks set parameters from a viewer click.

use lumiere_motion_engine::Session;
use lumiere_motion_engine::keyframe::Value as KV;
use lumiere_motion_engine::project::{Layer, LayerId};
use lumiere_motion_ui_egui::LumiereMotionApp;
use lumiere_motion_ui_egui::dock::PanelKind;
use lumiere_motion_ui_egui::state::FxPick;
use egui_kittest::Harness;
use serde_json::json;

struct Ids {
    small: u64,
    point: u64,
    color: u64,
    angle: u64,
    layer_ctl: u64,
    curves: u64,
    levels: u64,
    point_fx: u64,
}

fn layer(s: &Session, id: u64) -> Layer {
    s.active_comp().unwrap().layer(LayerId(id)).unwrap().clone()
}

/// A 640×360 comp: a full-frame green solid under a 200×100 solid scaled 200% at the centre,
/// carrying controls, Curves and Levels (Individual Controls).
fn app() -> (LumiereMotionApp, Ids) {
    let mut s = Session::default();
    s.execute("comp.new", json!({"name": "Main", "width": 640, "height": 360, "frameRate": 30, "duration": 10})).unwrap();
    s.execute("layer.newSolid", json!({"name": "Back", "color": "#20c040"})).unwrap();
    let small = s.execute("layer.newSolid", json!({"name": "Small", "color": "#ff0000", "width": 200, "height": 100})).unwrap()["layer"].as_u64().unwrap();
    s.execute("prop.set", json!({"layer": small, "path": "transform/scale", "value": [200.0, 200.0]})).unwrap();
    let mut fx = |name: &str| s.execute("effect.apply", json!({"layers": [small], "effect": name})).unwrap()["effects"][0].as_u64().unwrap();
    let point_fx = fx("Point Control");
    fx("Color Control");
    fx("Angle Control");
    fx("Layer Control");
    let curves = fx("Curves");
    let levels = fx("Levels (Individual Controls)");
    let l = layer(&s, small);
    let fxg = l.effects().unwrap();
    let param = |effect: u64, id: &str| fxg.groups().find(|g| g.uid == effect).and_then(|g| g.get(id)).map(|p| p.uid).unwrap();
    let find = |m: &str, id: &str| fxg.groups().find(|g| g.match_id == m).and_then(|g| g.get(id)).map(|p| p.uid).unwrap();
    let ids = Ids {
        small,
        point: param(point_fx, "point"),
        color: find("ec.control.color", "color"),
        angle: find("ec.control.angle", "angle"),
        layer_ctl: find("ec.control.layer", "layer"),
        curves,
        levels,
        point_fx,
    };
    s.state.selected_layers = vec![LayerId(small)];
    s.state.selected_props.clear();
    let mut app = LumiereMotionApp::new(s);
    app.show_panel(PanelKind::EffectControls);
    (app, ids)
}

fn settle(h: &mut Harness<'_, LumiereMotionApp>) {
    for _ in 0..600 {
        h.step();
        if h.state().frames.inflight() == 0 && h.state().frames.last_ms.lock().map(|v| *v > 0.0).unwrap_or(false) {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(20));
    }
    for _ in 0..4 {
        h.step();
    }
}

fn ids(h: &Harness<'_, LumiereMotionApp>) -> Vec<String> {
    h.state().auto.previous.iter().chain(h.state().auto.elements.iter()).map(|e| e.id.clone()).collect()
}

fn click(h: &mut Harness<'_, LumiereMotionApp>, pos: egui::Pos2) {
    h.event(egui::Event::PointerMoved(pos));
    h.step();
    h.event(egui::Event::PointerButton { pos, button: egui::PointerButton::Primary, pressed: true, modifiers: Default::default() });
    h.step();
    h.event(egui::Event::PointerButton { pos, button: egui::PointerButton::Primary, pressed: false, modifiers: Default::default() });
    h.step();
    h.step();
}

#[test]
fn effect_controls_widgets_register_automation_ids() {
    let (app, x) = app();
    let mut h = Harness::builder().with_size(egui::vec2(1700.0, 1100.0)).build_eframe(|_| app);
    settle(&mut h);
    let have = ids(&h);
    let want = [
        format!("effectControls.prop.{}.crosshair", x.point),
        format!("effectControls.prop.{}.eyedropper", x.color),
        format!("effectControls.prop.{}.twirl", x.angle),
        format!("effectControls.prop.{}.source", x.layer_ctl),
        format!("effectControls.effect.{}.curves.graph", x.curves),
        format!("effectControls.effect.{}.curves.channel", x.curves),
        format!("effectControls.effect.{}.curves.reset", x.curves),
        format!("effectControls.effect.{}.reset", x.point_fx),
    ];
    let missing: Vec<&String> = want.iter().filter(|w| !have.contains(w)).collect();
    assert!(missing.is_empty(), "missing {missing:?}");
    // Levels sits below: collapse Curves to bring it into view.
    h.state_mut().ui.fx_closed.insert(x.curves);
    h.step();
    h.step();
    let have = ids(&h);
    let want =
        ["histogram", "channel", "inBlack", "gamma", "inWhite", "outBlack", "outWhite"].map(|k| format!("effectControls.effect.{}.levels.{k}", x.levels));
    let missing: Vec<&String> = want.iter().filter(|w| !have.contains(w)).collect();
    assert!(missing.is_empty(), "missing {missing:?}");
    // The angle reads AE-style.
    let angle =
        h.state().auto.previous.iter().chain(h.state().auto.elements.iter()).find(|e| e.id == format!("effectControls.prop.{}.value", x.angle)).cloned();
    assert_eq!(angle.map(|e| e.label), Some("0x+0.0°".to_string()));
    // Twirling the angle open shows its dial.
    h.state_mut().ui.fx_slider_open.insert(x.angle);
    h.step();
    h.step();
    assert!(ids(&h).contains(&format!("effectControls.prop.{}.dial", x.angle)));
    // No effect selected: no on-viewer point control. Selecting the effect shows it.
    let pt_id = format!("viewer.effectPoint.{}", x.point);
    assert!(!ids(&h).contains(&pt_id));
    h.state_mut().session.state.selected_props = vec![(LayerId(x.small), x.point_fx)];
    h.step();
    h.step();
    assert!(ids(&h).contains(&pt_id), "effect point control shown for the selected effect");
}

#[test]
fn crosshair_and_eyedropper_pick_from_the_viewer() {
    let (app, x) = app();
    let mut h = Harness::builder().with_size(egui::vec2(1700.0, 1100.0)).build_eframe(|_| app);
    settle(&mut h);
    // Crosshair: a click at comp (330, 190) is layer (105, 55) on the 200%-scaled layer
    // (anchor (100, 50) at position (320, 180)).
    h.state_mut().ui.fx_pick = Some(FxPick { kind: "point".into(), layer: x.small, prop: x.point, name: "Point".into() });
    h.step();
    assert!(ids(&h).contains(&"viewer.fxPick".to_string()));
    let pos = lumiere_motion_ui_egui::panels::viewer::comp_to_screen(&h.ctx, [330.0, 190.0]).unwrap();
    click(&mut h, pos);
    assert!(h.state().ui.fx_pick.is_none(), "the pick is used up");
    let v = layer(&h.state().session, x.small).effects().unwrap().find(x.point).unwrap().value.clone();
    let KV::Vec2(p) = v else { panic!("{v:?}") };
    // One viewer point is up to 1/zoom comp pixels.
    let tol = 1.0 / lumiere_motion_ui_egui::panels::viewer::last_fit(&h.ctx) as f64;
    assert!((p[0] - 105.0).abs() <= tol && (p[1] - 55.0).abs() <= tol, "{p:?}");
    // Eyedropper over the green background.
    h.state_mut().ui.fx_pick = Some(FxPick { kind: "color".into(), layer: x.small, prop: x.color, name: "Color".into() });
    h.step();
    let pos = lumiere_motion_ui_egui::panels::viewer::comp_to_screen(&h.ctx, [24.0, 24.0]).unwrap();
    click(&mut h, pos);
    let v = layer(&h.state().session, x.small).effects().unwrap().find(x.color).unwrap().value.clone();
    let KV::Color(c) = v else { panic!("{v:?}") };
    let want = [0x20 as f64 / 255.0, 0xc0 as f64 / 255.0, 0x40 as f64 / 255.0];
    for k in 0..3 {
        assert!((c[k] - want[k]).abs() < 0.02, "{c:?} vs {want:?}");
    }
    assert_eq!(c[3], 1.0);
}

/// Key Light's Screen Colour eyedropper picks from the effect's input: the shown frame is
/// already keyed (the default screen colour takes most of a green), so sampling it would miss.
#[test]
fn keyer_eyedropper_picks_the_screen_from_the_effect_input() {
    let mut s = Session::default();
    s.execute("comp.new", json!({"name": "Key", "width": 320, "height": 180, "frameRate": 30, "duration": 1})).unwrap();
    let screen = s.execute("layer.newSolid", json!({"name": "Screen", "color": "#30b050"})).unwrap()["layer"].as_u64().unwrap();
    let fx = s.execute("effect.apply", json!({"layers": [screen], "effect": "Key Light"})).unwrap()["effects"][0].as_u64().unwrap();
    let prop = layer(&s, screen).effects().unwrap().groups().find(|g| g.uid == fx).and_then(|g| g.get("screenColour")).map(|p| p.uid).unwrap();
    s.state.selected_layers = vec![LayerId(screen)];
    let mut app = LumiereMotionApp::new(s);
    app.show_panel(PanelKind::EffectControls);
    let mut h = Harness::builder().with_size(egui::vec2(1700.0, 1100.0)).build_eframe(|_| app);
    settle(&mut h);
    h.state_mut().ui.fx_pick = Some(FxPick { kind: "color".into(), layer: screen, prop, name: "Screen Colour".into() });
    h.step();
    let pos = lumiere_motion_ui_egui::panels::viewer::comp_to_screen(&h.ctx, [160.0, 90.0]).unwrap();
    click(&mut h, pos);
    assert!(h.state().ui.fx_pick.is_none(), "the pick is used up");
    let v = layer(&h.state().session, screen).effects().unwrap().find(prop).unwrap().value.clone();
    let KV::Color(c) = v else { panic!("{v:?}") };
    let want = [0x30 as f64 / 255.0, 0xb0 as f64 / 255.0, 0x50 as f64 / 255.0];
    for k in 0..3 {
        assert!((c[k] - want[k]).abs() < 0.01, "{c:?} vs {want:?}");
    }
    // The picked screen keys the whole solid out.
    let s = &h.state().session;
    let img = s.render(s.active_comp_id().unwrap(), s.time(), Default::default());
    assert!(img.get(160, 90)[3] < 0.01, "{:?}", img.get(160, 90));
}
