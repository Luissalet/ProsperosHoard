//! Headless UI tests of the Sequence / Markers menu additions: the Delete Tracks dialog, the
//! through-edit marks on the timeline, the Markers panel colour filter and the Shift+; gap key.
//! The real `LumiereEditApp` under `egui_kittest`, driven over the control channel by automation id.

use std::sync::mpsc::{Sender, channel};

use egui_kittest::Harness;
use lumiere_codecbase_engine::Session;
use lumiere_codecbase_ui_egui::LumiereEditApp;
use lumiere_codecbase_ui_egui::control::ControlRequest;
use serde_json::{Value, json};

struct Driver {
    harness: Harness<'static, LumiereEditApp>,
    tx: Sender<ControlRequest>,
}

impl Driver {
    fn demo() -> Self {
        let mut session = Session::default();
        session.execute("file.openDemoProject", json!({})).expect("demo project");
        let (tx, rx) = channel();
        let app = LumiereEditApp::new(session).with_control(rx);
        let harness = Harness::builder().with_size(egui::vec2(1600.0, 980.0)).with_max_steps(10_000).build_eframe(move |_cc| app);
        let mut d = Driver { harness, tx };
        d.frames(4);
        d
    }

    fn frames(&mut self, n: usize) {
        for _ in 0..n {
            let ctx = self.harness.ctx.clone();
            let mut raw = std::mem::take(self.harness.input_mut());
            eframe::App::raw_input_hook(self.harness.state_mut(), &ctx, &mut raw);
            *self.harness.input_mut() = raw;
            self.harness.step();
        }
    }

    fn call(&mut self, method: &str, params: Value) -> Value {
        let (req, reply) = ControlRequest::new(method, params.clone());
        self.tx.send(req).unwrap();
        for _ in 0..600 {
            self.frames(1);
            if let Ok(v) = reply.try_recv() {
                return v;
            }
        }
        panic!("no reply to {method} {params}");
    }

    fn ok(&mut self, method: &str, params: Value) -> Value {
        let v = self.call(method, params.clone());
        assert_eq!(v["ok"], json!(true), "{method} {params} failed: {v}");
        v["result"].clone()
    }

    fn exec(&mut self, command: &str, params: Value) -> Value {
        self.ok("engine.execute", json!({"command": command, "params": params}))
    }

    fn click(&mut self, id: &str) {
        self.ok("ui.click", json!({"id": id}));
        self.frames(2);
    }

    fn ids(&mut self, prefix: &str) -> Vec<String> {
        let v = self.ok("ui.elements", json!({"prefix": prefix}));
        v.as_array().unwrap().iter().filter_map(|e| e["id"].as_str().map(str::to_string)).collect()
    }
}

#[test]
fn delete_tracks_dialog_deletes_empty_tracks() {
    let mut d = Driver::demo();
    let before = d.exec("sequence.inspect", json!({}));
    let nv = before["video"].as_array().unwrap().len();
    let na = before["audio"].as_array().unwrap().len();
    let r = d.ok("ui.menu.invoke", json!({"id": "sequence.deleteTracks"}));
    assert_eq!(r["dialog"], "deleteTracks", "{r}");
    d.frames(3);
    let ids = d.ids("deleteTracks.");
    for id in ["deleteTracks.video", "deleteTracks.audio", "deleteTracks.video.target", "deleteTracks.audio.target", "deleteTracks.ok", "deleteTracks.cancel"] {
        assert!(ids.iter().any(|i| i == id), "{id} missing: {ids:?}");
    }
    d.click("deleteTracks.video");
    d.click("deleteTracks.audio");
    d.click("deleteTracks.ok");
    assert!(d.ids("deleteTracks.").is_empty(), "dialog closed");
    let after = d.exec("sequence.inspect", json!({}));
    let (nv2, na2) = (after["video"].as_array().unwrap().len(), after["audio"].as_array().unwrap().len());
    assert!(nv2 < nv && na2 < na, "empty tracks deleted: {nv}->{nv2}, {na}->{na2}");
    // Cancel leaves the sequence alone
    d.ok("ui.menu.invoke", json!({"id": "sequence.deleteTracks"}));
    d.frames(3);
    d.click("deleteTracks.video");
    d.click("deleteTracks.cancel");
    assert_eq!(d.exec("sequence.inspect", json!({}))["video"].as_array().unwrap().len(), nv2);
}

#[test]
fn through_edits_are_marked_on_the_timeline() {
    let mut d = Driver::demo();
    d.exec("playhead.set", json!({"frame": 30}));
    d.exec("sequence.addEditAllTracks", json!({}));
    d.frames(3);
    assert!(d.ids("timeline.throughEdit.").is_empty(), "hidden until Show Through Edits");
    d.ok("ui.menu.invoke", json!({"id": "sequence.showThroughEdits"}));
    d.frames(3);
    let marks = d.ids("timeline.throughEdit.");
    assert!(marks.len() >= 2, "V1 + A1 marks: {marks:?}");
    d.exec("sequence.joinThroughEdits", json!({"all": true}));
    d.frames(3);
    assert!(d.ids("timeline.throughEdit.").is_empty(), "joined");
}

#[test]
fn markers_panel_colour_filter() {
    let mut d = Driver::demo();
    d.exec("markers.clearAll", json!({}));
    d.exec("markers.add", json!({"frame": 10, "name": "green"}));
    d.exec("markers.add", json!({"frame": 20, "name": "red", "color": "Rose"}));
    d.ok("ui.panel.show", json!({"panel": "Markers"}));
    d.frames(3);
    assert_eq!(d.ids("markers.row.").len(), 2);
    assert!(d.ids("markers.filter.").len() >= 7);
    d.click("markers.filter.Rose");
    d.frames(2);
    assert_eq!(d.ids("markers.row.").len(), 1, "red hidden");
    let m = d.ok("ui.menu.list", json!({}));
    let show_all = m.as_array().unwrap().iter().find(|i| i["id"] == "markers.showAllMarkerColors").unwrap().clone();
    assert_eq!(show_all["enabled"], json!(true));
    d.ok("ui.menu.invoke", json!({"id": "markers.showAllMarkerColors"}));
    d.frames(3);
    assert_eq!(d.ids("markers.row.").len(), 2);
}

#[test]
fn shift_semicolon_goes_to_the_next_gap() {
    let mut d = Driver::demo();
    d.exec("markers.markIn", json!({"frame": 48}));
    d.exec("markers.markOut", json!({"frame": 71}));
    d.exec("sequence.lift", json!({}));
    d.exec("playhead.set", json!({"frame": 0}));
    // the key a US layout sends for Shift+; is `:`
    d.harness.input_mut().events.push(egui::Event::Key {
        key: egui::Key::Colon,
        physical_key: Some(egui::Key::Semicolon),
        pressed: true,
        repeat: false,
        modifiers: egui::Modifiers::SHIFT,
    });
    d.frames(3);
    let ph = d.exec("sequence.inspect", json!({}))["playhead"].clone();
    let want = d.exec("playhead.set", json!({"frame": 48}))["time"].clone();
    assert_eq!(ph, want);
}
