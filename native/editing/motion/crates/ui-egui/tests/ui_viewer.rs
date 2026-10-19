//! Headless checks for the Composition viewer's interaction parity: bottom bar order, region of
//! interest drawing, rulers and guides, snapping, the shape Pen and motion-path key drags
//! (egui_kittest, UI logic only).

use lumiere_motion_engine::Session;
use lumiere_motion_engine::project::LayerId;
use lumiere_motion_ui_egui::LumiereMotionApp;
use lumiere_motion_ui_egui::state::Tool;
use egui::{Event, Pos2, Rect, pos2, vec2};
use egui_kittest::Harness;
use serde_json::json;

fn app() -> LumiereMotionApp {
    let mut s = Session::default();
    s.execute("comp.new", json!({"name": "View", "width": 640, "height": 360, "duration": 4})).unwrap();
    s.execute("layer.newSolid", json!({"name": "Plate", "color": "#406080", "width": 640, "height": 360})).unwrap();
    s.execute("layer.newSolid", json!({"name": "Box", "color": "#e04020", "width": 80, "height": 80})).unwrap();
    s.execute("edit.deselectAll", json!({})).unwrap();
    LumiereMotionApp::new(s)
}

fn harness() -> Harness<'static, LumiereMotionApp> {
    let mut h = Harness::builder().with_size(egui::vec2(1600.0, 1000.0)).build_eframe(|_| app());
    h.run_steps(3);
    h
}

fn rect(h: &Harness<'_, LumiereMotionApp>, id: &str) -> Rect {
    let e = h.state().auto.find(id).unwrap_or_else(|| panic!("no {id}"));
    Rect::from_min_size(pos2(e.rect[0], e.rect[1]), vec2(e.rect[2], e.rect[3]))
}

fn click(h: &mut Harness<'_, LumiereMotionApp>, p: Pos2) {
    h.input_mut().events.push(Event::PointerMoved(p));
    h.step();
    h.input_mut().events.push(Event::PointerButton { pos: p, button: egui::PointerButton::Primary, pressed: true, modifiers: Default::default() });
    h.step();
    h.input_mut().events.push(Event::PointerButton { pos: p, button: egui::PointerButton::Primary, pressed: false, modifiers: Default::default() });
    h.run_steps(2);
}

fn drag(h: &mut Harness<'_, LumiereMotionApp>, from: Pos2, to: Pos2) {
    h.input_mut().events.push(Event::PointerMoved(from));
    h.input_mut().events.push(Event::PointerButton { pos: from, button: egui::PointerButton::Primary, pressed: true, modifiers: Default::default() });
    h.step();
    for i in 1..=8 {
        h.input_mut().events.push(Event::PointerMoved(from + (to - from) * (i as f32 / 8.0)));
        h.step();
    }
    h.input_mut().events.push(Event::PointerButton { pos: to, button: egui::PointerButton::Primary, pressed: false, modifiers: Default::default() });
    h.run_steps(2);
}

/// Comp pixel → screen point.
fn screen(h: &Harness<'_, LumiereMotionApp>, p: [f32; 2]) -> Pos2 {
    let c = rect(h, "viewer.comp");
    let z = c.width() / 640.0;
    c.min + vec2(p[0] * z, p[1] * z)
}

fn selection_harness() -> Harness<'static, LumiereMotionApp> {
    let mut s = Session::default();
    s.execute("comp.new", json!({"name": "Selection", "width": 640, "height": 360, "duration": 4})).unwrap();
    for name in ["Back", "Front"] {
        s.execute("layer.newSolid", json!({"name": name, "color": "#406080", "width": 80, "height": 80})).unwrap();
    }
    s.execute("edit.deselectAll", json!({})).unwrap();
    let mut h = Harness::builder().with_size(egui::vec2(1600.0, 1000.0)).build_eframe(move |_| LumiereMotionApp::new(s));
    h.run_steps(3);
    h
}

#[test]
fn viewer_click_skips_hidden_and_unsoloed_layers() {
    for switch in ["video", "solo"] {
        let mut h = selection_harness();
        let comp = h.state().session.active_comp().unwrap();
        let (front, back) = (comp.layers[0].id, comp.layers[1].id);
        let at = screen(&h, [320.0, 180.0]);
        click(&mut h, at);
        assert_eq!(h.state().session.state.selected_layers, vec![front]);
        let s = &mut h.state_mut().session;
        let (target, value) = if switch == "video" { (front, false) } else { (back, true) };
        s.execute("layer.setSwitch", json!({"layers": [target.0], "switch": switch, "value": value})).unwrap();
        s.execute("edit.deselectAll", json!({})).unwrap();
        h.run_steps(40); // Start a new click rather than a double-click.
        click(&mut h, at);
        assert_eq!(h.state().session.state.selected_layers, vec![back], "{switch}");
    }
}

#[test]
fn viewer_marquee_skips_hidden_and_unsoloed_layers() {
    for switch in ["video", "solo"] {
        let mut h = selection_harness();
        let comp = h.state().session.active_comp().unwrap();
        let (front, back) = (comp.layers[0].id, comp.layers[1].id);
        let (target, value) = if switch == "video" { (front, false) } else { (back, true) };
        h.state_mut().session.execute("layer.setSwitch", json!({"layers": [target.0], "switch": switch, "value": value})).unwrap();
        h.run_steps(3);
        let (from, to) = (screen(&h, [250.0, 110.0]), screen(&h, [390.0, 250.0]));
        drag(&mut h, from, to);
        assert_eq!(h.state().session.state.selected_layers, vec![back], "{switch}");
    }
}

#[test]
fn viewer_click_does_not_pick_through_hidden_or_locked_solo_layers() {
    for switch in ["video", "lock"] {
        let mut h = selection_harness();
        let front = h.state().session.active_comp().unwrap().layers[0].id;
        let s = &mut h.state_mut().session;
        s.execute("layer.setSwitch", json!({"layers": [front.0], "switch": "solo", "value": true})).unwrap();
        s.execute("layer.setSwitch", json!({"layers": [front.0], "switch": switch, "value": switch == "lock"})).unwrap();
        h.run_steps(3);
        let at = screen(&h, [320.0, 180.0]);
        click(&mut h, at);
        assert!(h.state().session.state.selected_layers.is_empty(), "{switch}");
    }
}

#[test]
fn viewer_click_ignores_inactive_solo_layers() {
    let mut h = selection_harness();
    let comp = h.state().session.active_comp().unwrap();
    let (front, back) = (comp.layers[0].id, comp.layers[1].id);
    let s = &mut h.state_mut().session;
    s.execute("layer.setSwitch", json!({"layers": [front.0], "switch": "solo", "value": true})).unwrap();
    let cid = s.active_comp_id().unwrap();
    std::sync::Arc::make_mut(&mut s.project).comp_mut(cid).unwrap().layer_mut(front).unwrap().in_point = lumiere_motion_engine::time::Tick::from_seconds_f64(1.0);
    h.run_steps(3);
    let at = screen(&h, [320.0, 180.0]);
    click(&mut h, at);
    assert_eq!(h.state().session.state.selected_layers, vec![back]);
}

#[test]
fn bottom_bar_in_after_effects_order() {
    let h = harness();
    let order = [
        "viewer.magnification",
        "viewer.resolution",
        "viewer.transparency",
        "viewer.masks",
        "viewer.roi",
        "viewer.grid",
        "viewer.channel",
        "viewer.resetExposure",
        "viewer.exposure",
        "viewer.snapshot",
        "viewer.showSnapshot",
        "viewer.fastPreviews",
        "viewer.timecode",
    ];
    let xs: Vec<f32> = order.iter().map(|id| rect(&h, id).min.x).collect();
    assert!(xs.windows(2).all(|w| w[0] < w[1]), "{xs:?}");
    let mag = &h.state().auto.find("viewer.magnification").unwrap().label;
    assert_eq!(mag, "Magnification");
}

#[test]
fn region_of_interest_is_drawn_in_the_viewer() {
    let mut h = harness();
    let at = rect(&h, "viewer.roi").center();
    click(&mut h, at);
    assert!(h.state().ui.viewer.roi_draw);
    let (a, b) = (screen(&h, [100.0, 50.0]), screen(&h, [300.0, 250.0]));
    drag(&mut h, a, b);
    let r = h.state().session.state.region_of_interest.expect("roi");
    assert!((r[0] - 100.0).abs() <= 2.0 && (r[1] - 50.0).abs() <= 2.0 && (r[2] - 200.0).abs() <= 3.0 && (r[3] - 200.0).abs() <= 3.0, "{r:?}");
    assert!(!h.state().ui.viewer.roi_draw);
    h.run_steps(3);
    assert!(h.state().auto.find("viewer.regionOfInterest").is_some());
    // The button clears it again.
    let at = rect(&h, "viewer.roi").center();
    click(&mut h, at);
    assert!(h.state().session.state.region_of_interest.is_none());
}

#[test]
fn rulers_make_guides_and_guides_move() {
    let mut h = harness();
    h.state_mut().ui.viewer.rulers = true;
    h.run_steps(3);
    let top = rect(&h, "viewer.ruler.top");
    let to = screen(&h, [0.0, 120.0]);
    drag(&mut h, pos2(to.x + 200.0, top.center().y), pos2(to.x + 200.0, to.y));
    let g = h.state().session.active_comp().unwrap().guides.clone();
    assert_eq!(g.len(), 1);
    assert!(!g[0].vertical && (g[0].position - 120.0).abs() <= 2.0, "{g:?}");
    // Drag the guide down with the Selection tool.
    let from = pos2(screen(&h, [500.0, 0.0]).x, screen(&h, [0.0, g[0].position as f32]).y);
    let to2 = pos2(from.x, screen(&h, [0.0, 200.0]).y);
    drag(&mut h, from, to2);
    let g = h.state().session.active_comp().unwrap().guides.clone();
    assert!((g[0].position - 200.0).abs() <= 2.0, "{g:?}");
}

#[test]
fn layer_drag_snaps_to_comp_centre_and_ctrl_disables() {
    let mut h = harness();
    let box_id = h.state().session.active_comp().unwrap().layers[0].id;
    h.state_mut().session.execute("prop.set", json!({"layer": box_id.0, "path": "transform/position", "value": [100, 100, 0]})).unwrap();
    h.run_steps(2);
    // Grab the box at its centre and drop it 3 px from the comp centre: it snaps there.
    let from = screen(&h, [100.0, 100.0]);
    let to = screen(&h, [323.0, 182.0]);
    drag(&mut h, from, to);
    let pos = |h: &Harness<'_, LumiereMotionApp>| {
        let l = h.state().session.active_comp().unwrap().layer(box_id).unwrap().clone();
        l.props.prop("transform/position").unwrap().value.as_vec3()
    };
    let p = pos(&h);
    assert!((p[0] - 320.0).abs() < 0.01 && (p[1] - 180.0).abs() < 0.01, "{p:?}");
    // Snapping off: the same drag lands where the pointer is.
    h.state_mut().session.execute("view.snapping", json!({"value": false})).unwrap();
    h.state_mut().session.execute("prop.set", json!({"layer": box_id.0, "path": "transform/position", "value": [100, 100, 0]})).unwrap();
    h.run_steps(2);
    drag(&mut h, from, to);
    let p = pos(&h);
    assert!((p[0] - 320.0).abs() > 1.0, "{p:?}");
}

#[test]
fn shape_pen_draws_a_closed_shape_layer() {
    let mut h = harness();
    h.state_mut().ui.tool = Tool::Pen;
    h.run_steps(2);
    let n0 = h.state().session.active_comp().unwrap().layers.len();
    for p in [[200.0, 100.0], [400.0, 120.0], [300.0, 260.0], [200.0, 100.0]] {
        let at = screen(&h, p);
        click(&mut h, at);
    }
    let comp = h.state().session.active_comp().unwrap().clone();
    assert_eq!(comp.layers.len(), n0 + 1);
    let l = &comp.layers[0];
    assert!(matches!(l.source, lumiere_motion_engine::project::LayerSource::Shape));
    let g = l.props.sub("contents").unwrap().groups().next().unwrap();
    assert_eq!(g.name, "Shape 1");
    let path = g.sub("contents").unwrap().groups().next().unwrap();
    let sp = path.get("path").unwrap().value.as_path().unwrap().clone();
    assert_eq!(sp.vertices.len(), 3);
    assert!(sp.closed);
    assert!((sp.vertices[0][0] - (200.0 - 320.0)).abs() < 1.5, "{:?}", sp.vertices);
    // G cycles the Pen slot: Add Vertex, Delete Vertex, Convert Vertex, Mask Feather, Pen.
    let ctx = h.ctx.clone();
    let mut seen = vec![];
    for _ in 0..5 {
        lumiere_motion_ui_egui::menus::invoke(h.state_mut(), &ctx, "tool.pen", json!({})).unwrap();
        seen.push(h.state().ui.tool);
    }
    assert_eq!(seen, vec![Tool::PenAdd, Tool::PenDelete, Tool::PenConvert, Tool::MaskFeather, Tool::Pen]);
    // Add Vertex on the first segment.
    h.state_mut().ui.tool = Tool::PenAdd;
    h.run_steps(2);
    let at = screen(&h, [300.0, 110.0]);
    click(&mut h, at);
    let comp = h.state().session.active_comp().unwrap().clone();
    let sp = comp.layers[0]
        .props
        .sub("contents")
        .unwrap()
        .groups()
        .next()
        .unwrap()
        .sub("contents")
        .unwrap()
        .groups()
        .next()
        .unwrap()
        .get("path")
        .unwrap()
        .value
        .as_path()
        .unwrap()
        .clone();
    assert_eq!(sp.vertices.len(), 4);
}

#[test]
fn motion_path_key_drag_edits_that_key() {
    let mut h = harness();
    let box_id: LayerId = h.state().session.active_comp().unwrap().layers[0].id;
    let s = &mut h.state_mut().session;
    s.execute("layer.select", json!({"layers": [box_id.0]})).unwrap();
    s.execute("prop.addKey", json!({"layer": box_id.0, "path": "transform/position", "time": 0.0, "value": [100, 100, 0]})).unwrap();
    s.execute("prop.addKey", json!({"layer": box_id.0, "path": "transform/position", "time": 2.0, "value": [500, 100, 0]})).unwrap();
    s.execute("view.snapping", json!({"value": false})).unwrap();
    s.set_time(lumiere_motion_engine::time::Tick::from_seconds_f64(1.0));
    h.run_steps(3);
    assert!(h.state().auto.find(&format!("viewer.motionPath.{}.1", box_id.0)).is_some());
    let from = screen(&h, [500.0, 100.0]);
    let to2 = screen(&h, [500.0, 300.0]);
    drag(&mut h, from, to2);
    let l = h.state().session.active_comp().unwrap().layer(box_id).unwrap().clone();
    let k = &l.props.prop("transform/position").unwrap().keys;
    let v1 = k[1].value.as_vec3();
    assert!((v1[1] - 300.0).abs() < 2.0 && (v1[0] - 500.0).abs() < 2.0, "{v1:?}");
    assert_eq!(k[0].value.as_vec3(), [100.0, 100.0, 0.0]);
}

#[test]
fn graph_editor_transform_box_scales_selected_keys_in_time() {
    let mut h = harness();
    let box_id: LayerId = h.state().session.active_comp().unwrap().layers[0].id;
    let s = &mut h.state_mut().session;
    s.execute("layer.select", json!({"layers": [box_id.0]})).unwrap();
    for (t, v) in [(0.0, 0.0), (1.0, 50.0), (2.0, 100.0)] {
        s.execute("prop.addKey", json!({"layer": box_id.0, "path": "transform/opacity", "time": t, "value": v})).unwrap();
    }
    let uid = s.active_comp().unwrap().layer(box_id).unwrap().props.prop("transform/opacity").unwrap().uid;
    s.execute("prop.select", json!({"layer": box_id.0, "prop": uid})).unwrap();
    let keys: Vec<_> = [0.0, 1.0, 2.0].iter().map(|t| json!({"layer": box_id.0, "prop": uid, "time": t})).collect();
    s.execute("keys.select", json!({"keys": keys})).unwrap();
    h.state_mut().ui.timeline.graph_editor = true;
    h.run_steps(4);
    for id in ["timeline.graph.snap", "timeline.graph.reference", "timeline.graph.transformBox", "timeline.graph.transformBox.5"] {
        assert!(h.state().auto.find(id).is_some(), "missing {id}");
    }
    // Drag the right edge handle to the right: the keys spread out in time, the first stays.
    let r = rect(&h, "timeline.graph.transformBox.5");
    let k0 = rect(&h, &format!("timeline.graph.key.{uid}.0.0")).center();
    let k2 = rect(&h, &format!("timeline.graph.key.{uid}.0.2")).center();
    let to = r.center() + vec2((k2.x - k0.x) * 0.5, 0.0);
    drag(&mut h, r.center(), to);
    let p = h.state().session.active_comp().unwrap().layer(box_id).unwrap().props.prop("transform/opacity").unwrap().clone();
    let ts: Vec<f64> = p.keys.iter().map(|k| k.time.seconds()).collect();
    assert_eq!(ts[0], 0.0);
    assert!(ts[2] > 2.5 && ts[2] < 3.5, "{ts:?}");
    assert_eq!(p.keys.iter().map(|k| k.value.as_f64()).collect::<Vec<_>>(), vec![0.0, 50.0, 100.0]);
    let undo = h.state().session.history.undo.iter().filter(|(l, _)| l == "Transform Keyframes").count();
    assert_eq!(undo, 1, "one undo step per drag");
}

#[test]
fn timeline_alt_drag_scales_a_key_group_in_time() {
    let mut h = harness();
    let box_id: LayerId = h.state().session.active_comp().unwrap().layers[0].id;
    let s = &mut h.state_mut().session;
    s.execute("layer.select", json!({"layers": [box_id.0]})).unwrap();
    for (t, v) in [(0.0, 0.0), (1.0, 50.0), (2.0, 100.0)] {
        s.execute("prop.addKey", json!({"layer": box_id.0, "path": "transform/opacity", "time": t, "value": v})).unwrap();
    }
    let uid = s.active_comp().unwrap().layer(box_id).unwrap().props.prop("transform/opacity").unwrap().uid;
    s.execute("keys.selectAll", json!({"layers": [box_id.0]})).unwrap();
    let ctx = h.ctx.clone();
    lumiere_motion_ui_egui::menus::invoke(h.state_mut(), &ctx, "timeline.reveal.opacity", json!({})).unwrap();
    h.run_steps(4);
    let mut ks: Vec<Pos2> =
        h.state().auto.query(&format!("timeline.key.{uid}.")).iter().map(|e| pos2(e.rect[0] + e.rect[2] / 2.0, e.rect[1] + e.rect[3] / 2.0)).collect();
    ks.sort_by(|a, b| a.x.total_cmp(&b.x));
    assert_eq!(ks.len(), 3, "{ks:?}");
    let (k0, k2) = (ks[0], ks[2]);
    // Alt-drag the last key to where 3 s would be: the group scales by 1.5 about the first key.
    let to = pos2(k0.x + (k2.x - k0.x) * 1.5, k2.y);
    let alt = egui::Modifiers { alt: true, ..Default::default() };
    h.input_mut().events.push(Event::PointerMoved(k2));
    h.step();
    h.input_mut().events.push(Event::ModifiersChanged(alt));
    h.input_mut().events.push(Event::PointerButton { pos: k2, button: egui::PointerButton::Primary, pressed: true, modifiers: alt });
    h.step();
    for i in 1..=8 {
        h.input_mut().events.push(Event::PointerMoved(k2 + (to - k2) * (i as f32 / 8.0)));
        h.step();
    }
    h.input_mut().events.push(Event::PointerButton { pos: to, button: egui::PointerButton::Primary, pressed: false, modifiers: alt });
    h.run_steps(2);
    let p = h.state().session.active_comp().unwrap().layer(box_id).unwrap().props.prop("transform/opacity").unwrap().clone();
    let ts: Vec<f64> = p.keys.iter().map(|k| k.time.seconds()).collect();
    assert_eq!(ts.len(), 3);
    assert_eq!(ts[0], 0.0);
    assert!((ts[2] - 3.0).abs() < 0.1 && (ts[1] - 1.5).abs() < 0.1, "{ts:?}");
}

#[test]
fn mask_feather_tool_adds_and_drags_feather_points() {
    let mut h = harness();
    // A mask on the Box (80×80 at the comp centre, comp 280…360 × 140…220): layer 10…70.
    let (box_id, mask) = {
        let s = &mut h.state_mut().session;
        let b = s.active_comp().unwrap().layers.iter().find(|l| l.name == "Box").unwrap().id;
        let m = s.execute("mask.new", json!({"layer": b.0, "vertices": [[10, 10], [70, 10], [70, 70], [10, 70]], "closed": true})).unwrap()["mask"]
            .as_u64()
            .unwrap();
        s.execute("layer.select", json!({"layers": [b.0]})).unwrap();
        (b, m)
    };
    h.state_mut().ui.tool = Tool::MaskFeather;
    h.run_steps(3);
    // Press on the top edge (comp 320,150) and drag 12 px up: an outer feather point of ≈12 px.
    let from = screen(&h, [320.0, 150.0]);
    let z = rect(&h, "viewer.comp").width() / 640.0;
    drag(&mut h, from, from - vec2(0.0, 12.0 * z));
    let pts = h.state_mut().session.execute("mask.featherPoint.list", json!({"layer": box_id.0, "mask": mask})).unwrap();
    let r = pts["points"][0]["radius"].as_f64().unwrap();
    assert!((r - 12.0).abs() < 1.5, "{pts}");
    assert_eq!(pts["points"][0]["segment"], json!(0));
    // The handle is registered; dragging it inwards makes it an inner feather point.
    h.run_steps(2);
    let handle = rect(&h, &format!("viewer.mask.{mask}.feather.0")).center();
    let to = screen(&h, [320.0, 160.0]);
    drag(&mut h, handle, to);
    let pts = h.state_mut().session.execute("mask.featherPoint.list", json!({"layer": box_id.0, "mask": mask})).unwrap();
    let r = pts["points"][0]["radius"].as_f64().unwrap();
    assert!((r + 10.0).abs() < 1.5, "{pts}");
    assert_eq!(pts["points"].as_array().unwrap().len(), 1);
    // One drag = one undo step each.
    h.state_mut().session.execute("edit.undo", json!({})).unwrap();
    let pts = h.state_mut().session.execute("mask.featherPoint.list", json!({"layer": box_id.0, "mask": mask})).unwrap();
    assert!(pts["points"][0]["radius"].as_f64().unwrap() > 0.0);
}

#[test]
fn region_of_interest_resizes_by_its_handles() {
    let mut h = harness();
    h.state_mut().session.execute("view.setRegionOfInterest", json!({"rect": [100, 50, 200, 200]})).unwrap();
    h.run_steps(3);
    // Bottom-right corner (handle 2) to (400, 300): the top-left stays.
    let c = rect(&h, "viewer.regionOfInterest.handle.2").center();
    let to = screen(&h, [400.0, 300.0]);
    drag(&mut h, c, to);
    let r = h.state().session.state.region_of_interest.expect("roi");
    assert!((r[0] - 100.0).abs() <= 1.0 && (r[1] - 50.0).abs() <= 1.0 && (r[2] - 300.0).abs() <= 2.0 && (r[3] - 250.0).abs() <= 2.0, "{r:?}");
    // The left edge (handle 7) only moves x.
    h.run_steps(2);
    let c = rect(&h, "viewer.regionOfInterest.handle.7").center();
    let to = pos2(screen(&h, [150.0, 0.0]).x, c.y + 40.0);
    drag(&mut h, c, to);
    let r = h.state().session.state.region_of_interest.expect("roi");
    assert!((r[0] - 150.0).abs() <= 1.0 && (r[1] - 50.0).abs() <= 1.0 && (r[2] - 250.0).abs() <= 2.0 && (r[3] - 250.0).abs() <= 2.0, "{r:?}");
}

#[test]
fn pan_behind_snaps_the_anchor_point() {
    let mut h = harness();
    let box_id = h.state().session.active_comp().unwrap().layers[0].id;
    h.state_mut().session.execute("prop.set", json!({"layer": box_id.0, "path": "transform/position", "value": [100, 100, 0]})).unwrap();
    h.state_mut().session.execute("layer.select", json!({"layers": [box_id.0]})).unwrap();
    h.state_mut().ui.tool = Tool::PanBehind;
    h.run_steps(2);
    // Drag the anchor (at the box centre) to 3 px from the comp centre: it snaps there, and the
    // position follows so the box doesn't move.
    let from = screen(&h, [100.0, 100.0]);
    let to = screen(&h, [323.0, 182.0]);
    // (The gesture applies the pointer of the previous frame: hold the end point a frame.)
    h.input_mut().events.push(Event::PointerMoved(from));
    h.input_mut().events.push(Event::PointerButton { pos: from, button: egui::PointerButton::Primary, pressed: true, modifiers: Default::default() });
    h.step();
    for i in 1..=10 {
        h.input_mut().events.push(Event::PointerMoved(from + (to - from) * (i.min(8) as f32 / 8.0)));
        h.step();
    }
    h.input_mut().events.push(Event::PointerButton { pos: to, button: egui::PointerButton::Primary, pressed: false, modifiers: Default::default() });
    h.run_steps(2);
    let l = h.state().session.active_comp().unwrap().layer(box_id).unwrap().clone();
    let p = l.props.prop("transform/position").unwrap().value.as_vec3();
    let a = l.props.prop("transform/anchor").unwrap().value.as_vec3();
    assert!((p[0] - 320.0).abs() < 0.01 && (p[1] - 180.0).abs() < 0.01, "anchor point snapped to the comp centre: {p:?}");
    assert!((a[0] - 260.0).abs() < 0.01 && (a[1] - 120.0).abs() < 0.01, "{a:?}");
}

#[test]
fn reference_axes_toggle_from_the_grid_menu() {
    let mut h = harness();
    // A 3D layer makes the axes relevant.
    let box_id = h.state().session.active_comp().unwrap().layers[0].id;
    h.state_mut().session.execute("layer.setSwitch", json!({"layers": [box_id.0], "switch": "threeD", "value": true})).unwrap();
    h.run_steps(3);
    assert!(h.state().auto.find("viewer.referenceAxes").is_some(), "on by default (Settings ▸ 3D)");
    // Issue #64: one compass, not two.
    assert_eq!(h.state().auto.query("viewer.referenceAxes").len(), 1);
    let g = rect(&h, "viewer.grid").center();
    click(&mut h, g);
    h.run_steps(2);
    let item = rect(&h, "viewer.gridItem.6").center();
    click(&mut h, item);
    h.run_steps(3);
    assert!(!h.state().session.prefs.three_d.show_reference_axes);
    assert!(h.state().auto.find("viewer.referenceAxes").is_none());
}

/// A Box layer pinned with two Position pins and a Bend pin in the middle (Bend tool active).
fn puppet_harness() -> (Harness<'static, LumiereMotionApp>, u64, u64) {
    let mut h = harness();
    let s = &mut h.state_mut().session;
    // No full-frame Plate behind the Box: the comp around the Box is empty, like the canvas
    // around a character.
    s.execute("edit.clear", json!({"layers": ["Plate"]})).unwrap();
    s.execute("layer.select", json!({"layers": ["Box"]})).unwrap();
    let id = s.state.selected_layers[0].0;
    s.execute("puppet.addPin", json!({"layer": id, "position": [10, 40]})).unwrap();
    s.execute("puppet.addPin", json!({"layer": id, "position": [70, 40]})).unwrap();
    let bend = s.execute("puppet.addPin", json!({"layer": id, "kind": "bend", "position": [40, 40]})).unwrap()["pin"].as_u64().unwrap();
    h.state_mut().ui.tool = Tool::PuppetBend;
    h.run_steps(3);
    (h, id, bend)
}

fn pin_value(h: &Harness<'_, LumiereMotionApp>, layer: u64, pin: u64, prop: &str) -> f64 {
    let l = h.state().session.active_comp().unwrap().layer(LayerId(layer)).unwrap().clone();
    l.props.find_group(pin).unwrap().get(prop).unwrap().value.as_f64()
}

#[test]
fn puppet_bend_pin_rotates_by_its_ring_and_scales_by_its_square() {
    let (mut h, id, bend) = puppet_harness();
    let center = rect(&h, &format!("viewer.puppetPin.{bend}")).center();
    let ring = rect(&h, &format!("viewer.puppetPin.{bend}.rotate")).center();
    let square = rect(&h, &format!("viewer.puppetPin.{bend}.scale")).center();
    // At 0° the square is on the right of the ring, the rotate target opposite it.
    assert!(square.x > center.x + 7.0 && (square.y - center.y).abs() < 0.5, "{square:?} {center:?}");
    // Drag the ring a quarter turn clockwise (left → top): +90°, one undo step.
    let undo_before = h.state().session.history.undo.len();
    drag(&mut h, ring, center + vec2(0.0, -(center.x - ring.x)));
    let r = pin_value(&h, id, bend, "rotation");
    assert!((r - 90.0).abs() < 1.0, "rotation {r}");
    assert_eq!(h.state().session.history.undo.len(), undo_before + 1);
    // The square follows the rotation (now below the pin; the bend carries the pin itself a
    // little); dragging it twice as far from the pin scales 200 %.
    let center = rect(&h, &format!("viewer.puppetPin.{bend}")).center();
    let square = rect(&h, &format!("viewer.puppetPin.{bend}.scale")).center();
    assert!(square.y > center.y + 7.0, "{square:?}");
    drag(&mut h, square, center + (square - center) * 2.0);
    let sc = pin_value(&h, id, bend, "scale");
    assert!((sc - 200.0).abs() < 2.0, "scale {sc}");
}

#[test]
fn puppet_pin_click_selects_and_delete_removes_only_the_pins() {
    let (mut h, id, bend) = puppet_harness();
    let pin = |h: &Harness<'_, LumiereMotionApp>| h.state().session.state.selected_props.iter().map(|(_, u)| *u).collect::<Vec<_>>();
    // Clicking a pin selects it (instead of adding a pin on top of it).
    let at = rect(&h, &format!("viewer.puppetPin.{bend}")).center();
    click(&mut h, at);
    assert_eq!(pin(&h), vec![bend]);
    let info = h.state_mut().session.execute("puppet.info", json!({"layer": id})).unwrap();
    assert_eq!(info["meshes"][0]["pins"].as_array().unwrap().len(), 3, "no pin added");
    // Delete removes the pin; the layer stays.
    h.input_mut().events.push(Event::Key { key: egui::Key::Delete, physical_key: None, pressed: true, repeat: false, modifiers: Default::default() });
    h.run_steps(2);
    let info = h.state_mut().session.execute("puppet.info", json!({"layer": id})).unwrap();
    assert_eq!(info["meshes"][0]["pins"].as_array().unwrap().len(), 2);
    assert!(h.state().session.active_comp().unwrap().layer(LayerId(id)).is_some());
}

#[test]
fn puppet_marquee_selects_pins_and_alt_drag_works_over_the_art() {
    let (mut h, _id, bend) = puppet_harness();
    let p1 = rect(&h, &format!("viewer.puppetPin.{bend}")).center();
    let pins: Vec<u64> = h.state().auto.previous.iter().filter_map(|e| e.id.strip_prefix("viewer.puppetPin.").and_then(|r| r.parse().ok())).collect();
    assert_eq!(pins.len(), 3, "{pins:?}");
    // A box around every pin, started on empty canvas (outside all art).
    let comp = rect(&h, "viewer.comp");
    drag(&mut h, comp.min + vec2(4.0, 4.0), comp.max - vec2(4.0, 4.0));
    let mut sel: Vec<u64> = h.state().session.state.selected_props.iter().map(|(_, u)| *u).collect();
    sel.sort();
    let mut want = pins.clone();
    want.sort();
    assert_eq!(sel, want);
    // Alt-drag a small box over the art (around the Bend pin only).
    h.input_mut().events.push(Event::PointerMoved(p1 - vec2(6.0, 6.0)));
    h.input_mut().events.push(Event::PointerButton {
        pos: p1 - vec2(6.0, 6.0),
        button: egui::PointerButton::Primary,
        pressed: true,
        modifiers: egui::Modifiers::ALT,
    });
    h.step();
    for i in 1..=6 {
        h.input_mut().events.push(Event::PointerMoved(p1 - vec2(6.0, 6.0) + vec2(12.0, 12.0) * (i as f32 / 6.0)));
        h.step();
    }
    h.input_mut().events.push(Event::PointerButton {
        pos: p1 + vec2(6.0, 6.0),
        button: egui::PointerButton::Primary,
        pressed: false,
        modifiers: egui::Modifiers::ALT,
    });
    h.run_steps(2);
    let sel: Vec<u64> = h.state().session.state.selected_props.iter().map(|(_, u)| *u).collect();
    assert_eq!(sel, vec![bend]);
}

/// Drag with `button` from `from` to `to` in steps, then release.
fn drag_with(h: &mut Harness<'_, LumiereMotionApp>, button: egui::PointerButton, from: Pos2, to: Pos2) {
    h.input_mut().events.push(Event::PointerMoved(from));
    h.input_mut().events.push(Event::PointerButton { pos: from, button, pressed: true, modifiers: Default::default() });
    h.step();
    for i in 1..=8 {
        h.input_mut().events.push(Event::PointerMoved(from + (to - from) * (i as f32 / 8.0)));
        h.step();
    }
    h.input_mut().events.push(Event::PointerButton { pos: to, button, pressed: false, modifiers: Default::default() });
    h.run_steps(3);
}

#[test]
fn middle_and_hand_drags_pan_the_viewer_and_the_pan_stays_after_release() {
    let mut h = harness();
    let c = rect(&h, "viewer.comp").center();
    let before = h.state().ui.viewer.pan;
    // Middle-button drag with the Selection tool: pans, and stays panned after the release.
    drag_with(&mut h, egui::PointerButton::Middle, c, c + vec2(120.0, 60.0));
    let after = h.state().ui.viewer.pan;
    assert!((after[0] - before[0] - 120.0).abs() < 1.0 && (after[1] - before[1] - 60.0).abs() < 1.0, "{before:?} → {after:?}");
    assert_eq!(h.state().ui.tool, Tool::Selection, "the tool is unchanged");
    // The Hand tool's primary drag keeps its pan too.
    h.state_mut().ui.tool = Tool::Hand;
    h.run_steps(2);
    let c = rect(&h, "viewer.comp").center();
    drag_with(&mut h, egui::PointerButton::Primary, c, c - vec2(50.0, 30.0));
    let end = h.state().ui.viewer.pan;
    assert!((end[0] - after[0] + 50.0).abs() < 1.0 && (end[1] - after[1] + 30.0).abs() < 1.0, "{after:?} → {end:?}");
}

#[test]
fn timeline_rows_drag_to_reorder_layers() {
    let mut h = harness();
    let names = |h: &Harness<'_, LumiereMotionApp>| h.state().session.active_comp().unwrap().layers.iter().map(|l| l.name.clone()).collect::<Vec<_>>();
    assert_eq!(names(&h), ["Box", "Plate"]);
    let row = |h: &Harness<'_, LumiereMotionApp>, name: &str| {
        let l = h.state().session.active_comp().unwrap().layers.iter().find(|l| l.name == name).unwrap().id.0;
        rect(h, &format!("timeline.layer.{l}.row"))
    };
    // Drag "Box" (top) below "Plate": it becomes the bottom layer, in one undo step.
    let (from, plate) = (row(&h, "Box").center(), row(&h, "Plate"));
    let steps = h.state().session.history.undo.len();
    drag(&mut h, from, plate.center() + vec2(0.0, plate.height() * 0.4));
    assert_eq!(names(&h), ["Plate", "Box"]);
    assert_eq!(h.state().session.history.undo.len(), steps + 1);
    // Dropping a layer where it already is changes nothing.
    let b = row(&h, "Box").center();
    drag(&mut h, b, b + vec2(0.0, 3.0));
    assert_eq!(names(&h), ["Plate", "Box"]);
    assert_eq!(h.state().session.history.undo.len(), steps + 1);
    // And back to the top.
    let (from, top) = (row(&h, "Box").center(), row(&h, "Plate"));
    drag(&mut h, from, top.center() - vec2(0.0, top.height() * 0.4));
    assert_eq!(names(&h), ["Box", "Plate"]);
}

fn layer_id(h: &Harness<'_, LumiereMotionApp>, name: &str) -> u64 {
    h.state().session.active_comp().unwrap().layers.iter().find(|l| l.name == name).unwrap().id.0
}

/// Uid of a layer's property by match path (`transform/position`).
fn prop_uid(h: &Harness<'_, LumiereMotionApp>, layer: &str, path: &str) -> u64 {
    let id = layer_id(h, layer);
    h.state().session.active_comp().unwrap().layer(LayerId(id)).unwrap().props.prop(path).unwrap().uid
}

/// Is the property's row on screen in the Timeline?
fn shown(h: &Harness<'_, LumiereMotionApp>, uid: u64) -> bool {
    h.state().auto.find(&format!("timeline.prop.{uid}.stopwatch")).is_some()
}

fn key(h: &mut Harness<'_, LumiereMotionApp>, key: egui::Key, modifiers: egui::Modifiers) {
    h.input_mut().events.push(Event::Key { key, physical_key: None, pressed: true, repeat: false, modifiers });
    h.input_mut().events.push(Event::Key { key, physical_key: None, pressed: false, repeat: false, modifiers });
    h.run_steps(2);
}

#[test]
fn timeline_twirl_arrows_open_layers_and_groups() {
    let mut h = harness();
    let id = layer_id(&h, "Box");
    let transform = {
        let comp = h.state().session.active_comp().unwrap().clone();
        comp.layer(LayerId(id)).unwrap().props.sub("transform").unwrap().uid
    };
    let at = rect(&h, &format!("timeline.layer.{id}.twirl")).center();
    click(&mut h, at);
    assert!(h.state().ui.timeline.open_layers.contains(&id), "the layer twirls open");
    let at = rect(&h, &format!("timeline.group.{transform}.twirl")).center();
    click(&mut h, at);
    for p in ["anchor", "position", "scale", "rotation", "opacity"] {
        assert!(shown(&h, prop_uid(&h, "Box", &format!("transform/{p}"))), "{p} row");
    }
    // And closed again.
    let at = rect(&h, &format!("timeline.layer.{id}.twirl")).center();
    click(&mut h, at);
    h.run_steps(2);
    assert!(!h.state().ui.timeline.open_layers.contains(&id));
    assert!(!shown(&h, prop_uid(&h, "Box", "transform/position")));
}

#[test]
fn timeline_rename_commits_on_enter_and_on_click_away() {
    // The rename field (double-click a name, or Enter on a selected layer) used to grab the
    // keyboard every frame: clicking away or Enter could never leave it. (The harness can't
    // double-click — its frames are longer than a double-click — so this opens it with Enter.)
    let mut h = harness();
    h.state_mut().session.execute("layer.newNull", json!({"name": "Ctrl"})).unwrap();
    let id = layer_id(&h, "Ctrl");
    let name = |h: &Harness<'_, LumiereMotionApp>| h.state().session.active_comp().unwrap().layer(LayerId(id)).unwrap().name.clone();
    let rename = |h: &mut Harness<'_, LumiereMotionApp>| {
        h.state_mut().session.execute("layer.select", json!({"layers": [id]})).unwrap();
        h.run_steps(2);
        key(h, egui::Key::Enter, Default::default());
        h.run_steps(1);
        assert!(h.ctx.memory(|m| m.focused().is_some()), "the name field has focus");
    };
    // The whole name is selected: typing replaces it; Enter commits and frees the keyboard.
    rename(&mut h);
    h.input_mut().events.push(Event::Text("Rig".into()));
    h.step();
    key(&mut h, egui::Key::Enter, Default::default());
    assert_eq!(name(&h), "Rig");
    assert!(h.ctx.memory(|m| m.focused().is_none()), "the field let go of the keyboard");
    key(&mut h, egui::Key::P, Default::default());
    assert_eq!(h.state().ui.timeline.reveal, vec!["position"], "P works after renaming");
    // Clicking away commits too.
    rename(&mut h);
    h.input_mut().events.push(Event::Text("Hand".into()));
    h.step();
    let away = rect(&h, "viewer.comp").center();
    click(&mut h, away);
    assert_eq!(name(&h), "Hand");
    assert!(h.ctx.memory(|m| m.focused().is_none()));
    // Escape cancels.
    rename(&mut h);
    h.input_mut().events.push(Event::Text("Nope".into()));
    h.step();
    key(&mut h, egui::Key::Escape, Default::default());
    assert_eq!(name(&h), "Hand");
}

/// Press `k` twice within one frame (a quick double press: the harness' frames are long).
fn key_twice(h: &mut Harness<'_, LumiereMotionApp>, k: egui::Key) {
    for _ in 0..2 {
        h.input_mut().events.push(Event::Key { key: k, physical_key: None, pressed: true, repeat: false, modifiers: Default::default() });
        h.input_mut().events.push(Event::Key { key: k, physical_key: None, pressed: false, repeat: false, modifiers: Default::default() });
    }
    h.run_steps(2);
}

#[test]
fn property_shortcuts_reveal_and_key_transform_properties() {
    let mut h = harness();
    h.state_mut().session.execute("layer.select", json!({"layers": ["Box"]})).unwrap();
    h.run_steps(2);
    let t = |h: &Harness<'_, LumiereMotionApp>, p: &str| shown(h, prop_uid(h, "Box", &format!("transform/{p}")));
    let only = |h: &Harness<'_, LumiereMotionApp>, want: &[&str]| {
        for p in ["anchor", "position", "scale", "rotation", "opacity"] {
            assert_eq!(t(h, p), want.contains(&p), "{p} shown? want {want:?}");
        }
    };
    let none = egui::Modifiers::default();
    // P, then T: one property at a time; Shift+S adds Scale; P again hides.
    key(&mut h, egui::Key::P, none);
    only(&h, &["position"]);
    key(&mut h, egui::Key::T, none);
    only(&h, &["opacity"]);
    key(&mut h, egui::Key::S, egui::Modifiers::SHIFT);
    only(&h, &["scale", "opacity"]);
    key(&mut h, egui::Key::A, none);
    only(&h, &["anchor"]);
    key(&mut h, egui::Key::R, none);
    only(&h, &["rotation"]);
    // Alt+Shift+P: a Position keyframe at the current time; again removes it.
    let pos_keys = |h: &Harness<'_, LumiereMotionApp>| {
        let id = layer_id(h, "Box");
        h.state().session.active_comp().unwrap().layer(LayerId(id)).unwrap().props.prop("transform/position").unwrap().keys.len()
    };
    let alt_shift = egui::Modifiers { alt: true, shift: true, ..Default::default() };
    key(&mut h, egui::Key::P, alt_shift);
    assert_eq!(pos_keys(&h), 1);
    key(&mut h, egui::Key::P, alt_shift);
    assert_eq!(pos_keys(&h), 0);
    // U: only keyframed properties (Opacity animated); U again later hides; UU modified ones.
    h.state_mut().session.execute("prop.addKey", json!({"layer": "Box", "path": "transform/opacity", "time": 0, "value": 50})).unwrap();
    h.state_mut().session.execute("prop.addKey", json!({"layer": "Box", "path": "transform/opacity", "time": 1, "value": 100})).unwrap();
    h.run_steps(2);
    key(&mut h, egui::Key::U, none);
    only(&h, &["opacity"]);
    h.run_steps(40);
    key(&mut h, egui::Key::U, none);
    only(&h, &[]);
    h.state_mut().session.execute("prop.set", json!({"layer": "Box", "path": "transform/rotation", "value": 30})).unwrap();
    h.run_steps(40);
    key_twice(&mut h, egui::Key::U);
    assert!(t(&h, "opacity") && t(&h, "rotation"), "UU: animated and changed properties");
    assert!(!t(&h, "scale"), "an untouched property stays hidden");
    // Ctrl+`: twirl the selected layer open, then closed.
    h.run_steps(40);
    key(&mut h, egui::Key::Backtick, egui::Modifiers::COMMAND);
    let id = layer_id(&h, "Box");
    assert!(h.state().ui.timeline.open_layers.contains(&id) && h.state().ui.timeline.reveal.is_empty());
    key(&mut h, egui::Key::Backtick, egui::Modifiers::COMMAND);
    assert!(!h.state().ui.timeline.open_layers.contains(&id));
}

#[test]
fn double_press_shortcuts_reveal_their_second_set() {
    let mut h = harness();
    h.state_mut().session.execute("layer.select", json!({"layers": ["Box"]})).unwrap();
    h.state_mut().session.execute("prop.setExpression", json!({"layer": "Box", "path": "transform/scale", "expression": "value"})).unwrap();
    h.run_steps(2);
    let reveal = |h: &Harness<'_, LumiereMotionApp>| h.state().ui.timeline.reveal.clone();
    // EE: properties with expressions (Scale), replacing E's effects.
    key_twice(&mut h, egui::Key::E);
    assert_eq!(reveal(&h), vec!["expressions"]);
    assert!(shown(&h, prop_uid(&h, "Box", "transform/scale")));
    h.run_steps(40);
    // TT: Mask Opacity; MM: every mask property; a single M: Mask Path.
    key_twice(&mut h, egui::Key::T);
    assert_eq!(reveal(&h), vec!["maskOpacity"]);
    h.run_steps(40);
    key(&mut h, egui::Key::M, Default::default());
    assert_eq!(reveal(&h), vec!["maskPath"]);
    h.run_steps(40);
    key_twice(&mut h, egui::Key::M);
    assert_eq!(reveal(&h), vec!["masks"]);
    h.run_steps(40);
    // RR Time Remap, AA Material Options, PP paint/puppet, SS selected properties, FF missing effects.
    for (k, want) in
        [(egui::Key::R, "timeRemap"), (egui::Key::A, "material"), (egui::Key::P, "paint"), (egui::Key::S, "selected"), (egui::Key::F, "missingEffects")]
    {
        key_twice(&mut h, k);
        assert_eq!(reveal(&h), vec![want], "{k:?}{k:?}");
        h.run_steps(40);
    }
}

/// Drag through `path` with `modifiers` held (press at the first point, release at the last).
fn drag_path(h: &mut Harness<'_, LumiereMotionApp>, path: &[Pos2], modifiers: egui::Modifiers) {
    h.input_mut().events.push(Event::ModifiersChanged(modifiers));
    h.input_mut().events.push(Event::PointerMoved(path[0]));
    h.input_mut().events.push(Event::PointerButton { pos: path[0], button: egui::PointerButton::Primary, pressed: true, modifiers });
    h.step();
    for w in path.windows(2) {
        for i in 1..=6 {
            h.input_mut().events.push(Event::PointerMoved(w[0] + (w[1] - w[0]) * (i as f32 / 6.0)));
            h.step();
        }
    }
    let end = *path.last().unwrap();
    h.input_mut().events.push(Event::PointerButton { pos: end, button: egui::PointerButton::Primary, pressed: false, modifiers });
    h.run_steps(2);
    // The keys come up after the button, as a hand does.
    h.input_mut().events.push(Event::ModifiersChanged(Default::default()));
    h.step();
}

/// Issue #63: a bounding-box handle scales about the anchor point so the grabbed corner follows
/// the pointer, relative to the scale the drag began with: dragging through the anchor and back
/// out recovers (it used to stick at 0), edges scale one axis, Shift keeps the proportions.
#[test]
fn handle_drags_scale_about_the_anchor_and_follow_the_pointer() {
    let mut h = harness();
    let box_id = h.state().session.active_comp().unwrap().layers[0].id;
    let set = |h: &mut Harness<'_, LumiereMotionApp>, path: &str, v: serde_json::Value| {
        h.state_mut().session.execute("prop.set", json!({"layer": box_id.0, "path": path, "value": v})).unwrap();
    };
    let scale = |h: &Harness<'_, LumiereMotionApp>| {
        let l = h.state().session.active_comp().unwrap().layer(box_id).unwrap().clone();
        l.props.prop("transform/scale").unwrap().value.as_vec3()
    };
    let close = |a: [f64; 3], b: [f64; 2]| (a[0] - b[0]).abs() < 1.0 && (a[1] - b[1]).abs() < 1.0;
    h.state_mut().session.execute("layer.select", json!({"layers": [box_id.0]})).unwrap();
    // The 80×80 box is centred at (320, 180) with its anchor in the middle.
    h.state_mut().session.execute("view.snapping", json!({"value": false})).unwrap();
    h.run_steps(2);
    let undo0 = h.state().session.history.undo.len();
    // Bottom-right corner (360, 220) to (400, 260): twice as far from the anchor.
    let corner = rect(&h, &format!("viewer.handle.{}.2", box_id.0)).center();
    let path = [corner, screen(&h, [400.0, 260.0])];
    drag_path(&mut h, &path, Default::default());
    assert!(close(scale(&h), [200.0, 200.0]), "{:?}", scale(&h));
    assert_eq!(h.state().session.history.undo.len(), undo0 + 1, "one undo step per drag");
    // Through the anchor (scale ≈ 0) and back out to 1.5×: the layer follows, nothing sticks.
    set(&mut h, "transform/scale", json!([100, 100, 100]));
    h.run_steps(2);
    let corner = rect(&h, &format!("viewer.handle.{}.2", box_id.0)).center();
    let path = [corner, screen(&h, [320.0, 180.0]), screen(&h, [300.0, 170.0]), screen(&h, [380.0, 240.0])];
    drag_path(&mut h, &path, Default::default());
    assert!(close(scale(&h), [150.0, 150.0]), "{:?}", scale(&h));
    // Past the anchor the layer flips, as in After Effects.
    set(&mut h, "transform/scale", json!([100, 100, 100]));
    h.run_steps(2);
    let corner = rect(&h, &format!("viewer.handle.{}.2", box_id.0)).center();
    let path = [corner, screen(&h, [280.0, 140.0])];
    drag_path(&mut h, &path, Default::default());
    assert!(close(scale(&h), [-100.0, -100.0]), "{:?}", scale(&h));
    // The right edge (handle 5) scales x only.
    set(&mut h, "transform/scale", json!([100, 100, 100]));
    h.run_steps(2);
    let edge = rect(&h, &format!("viewer.handle.{}.5", box_id.0)).center();
    let path = [edge, screen(&h, [380.0, 200.0])];
    drag_path(&mut h, &path, Default::default());
    assert!(close(scale(&h), [150.0, 100.0]), "{:?}", scale(&h));
    // Shift on a corner keeps the proportions: the pointer's place along the diagonal.
    set(&mut h, "transform/scale", json!([100, 100, 100]));
    h.run_steps(2);
    let corner = rect(&h, &format!("viewer.handle.{}.2", box_id.0)).center();
    let path = [corner, screen(&h, [400.0, 230.0])];
    drag_path(&mut h, &path, egui::Modifiers::SHIFT);
    assert!(close(scale(&h), [162.5, 162.5]), "{:?}", scale(&h));
}
