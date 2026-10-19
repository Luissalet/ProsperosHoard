//! Headless UI tests of multi-camera editing: the Create Multi-Camera Source Sequence, Synchronize
//! and Merge Clips dialogs, the Program monitor's Multi-Camera view (angle grid, clicking an
//! angle, Ctrl-click for video only) and live switching with the 1–9 keys while playing.
//!
//! Set `FILMCRAFT_UI_SNAPSHOT_DIR=<dir>` to also render the window offscreen with wgpu and write PNGs
//! there (`multicam-*.png`).

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::mpsc::{Sender, channel};

use egui_kittest::Harness;
use lumiere_codecbase_engine::Session;
use lumiere_codecbase_engine::project::{ItemKind, Label, MediaClip, MediaRef, Project, SequenceSettings, TrackKind};
use lumiere_codecbase_media::generators::GeneratorSource;
use lumiere_codecbase_media::{DemoScene, Generator, MediaSource};
use lumiere_codecbase_time::{FrameRate, Tick, TimeRange};
use lumiere_codecbase_ui_egui::LumiereEditApp;
use lumiere_codecbase_ui_egui::control::ControlRequest;
use serde_json::{Value, json};

const W: u32 = 320;
const H: u32 = 180;

fn tmp_dir(name: &str) -> PathBuf {
    let nanos = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos();
    let d = std::env::temp_dir().join(format!("lumiere-codecbase-ui-{name}-{}-{nanos}", std::process::id()));
    std::fs::create_dir_all(&d).unwrap();
    d
}

/// A 24 fps ProRes movie of a demo scene (our own encoder).
fn make_movie(path: &Path, scene: DemoScene, frames: i64) {
    let rate = FrameRate::FPS_24;
    let dur = rate.tick_of(frames);
    let src = GeneratorSource::new(Generator::Demo(scene), W, H, rate, dur);
    let clip = MediaClip {
        media: MediaRef::Generator(Generator::Demo(scene)),
        info: src.info().clone(),
        interpret: Default::default(),
        mark_in: None,
        mark_out: None,
        markers: vec![],
        offline: false,
        proxy: None,
        identity: None,
    };
    let mut p = Project::new("fixture");
    let item = p.add_item("scene", Label::Iris, ItemKind::Media(clip), None);
    let seq = p.new_sequence("s", SequenceSettings { width: W, height: H, frame_rate: rate, ..Default::default() }, 1, 0, None);
    let mut v = p.make_track_item(item, TrackKind::Video, Tick::ZERO, TimeRange::new(Tick::ZERO, dur), rate).unwrap();
    for e in &mut v.effects {
        lumiere_codecbase_engine::project::resolve_auto_points(e, (W, H), (W, H));
    }
    p.sequence_mut(seq).unwrap().video_tracks[0].items.push(v);
    let settings = lumiere_codecbase_engine::export::ExportSettings {
        format: lumiere_codecbase_engine::export::Format::ProRes,
        path: path.to_string_lossy().into_owned(),
        include_audio: false,
        ..Default::default()
    };
    let shared: lumiere_codecbase_media::SharedSource = Arc::new(src);
    let provider = move |id| (id == item).then(|| shared.clone());
    lumiere_codecbase_engine::export::export(&Arc::new(p), seq, &settings, &provider, &Default::default()).unwrap();
}

/// Four cameras imported into a session with an empty 320×180 sequence "Cut".
fn session() -> (Session, Vec<u64>) {
    let dir = tmp_dir("multicam");
    let scenes = [DemoScene::OceanSunset, DemoScene::CityNight, DemoScene::Aurora, DemoScene::Forest];
    let files: Vec<PathBuf> = (0..4).map(|i| dir.join(format!("Cam {}.mov", (b'A' + i as u8) as char))).collect();
    for (f, sc) in files.iter().zip(scenes) {
        make_movie(f, sc, 96);
    }
    let mut s = Session::default();
    let r = s.execute("file.import", json!({"paths": files.iter().map(|f| f.to_string_lossy()).collect::<Vec<_>>()})).unwrap();
    let items: Vec<u64> = r["items"].as_array().unwrap().iter().map(|v| v.as_u64().unwrap()).collect();
    let mut p = (*s.project).clone();
    let seq = p.new_sequence("Cut", SequenceSettings { width: W, height: H, frame_rate: FrameRate::FPS_24, ..Default::default() }, 2, 2, None);
    s.project = Arc::new(p);
    s.state.active_sequence = Some(seq);
    s.state.open_sequences = vec![seq];
    (s, items)
}

struct Driver {
    harness: Harness<'static, LumiereEditApp>,
    tx: Sender<ControlRequest>,
    snapshots: Option<PathBuf>,
}

impl Driver {
    fn new(session: Session) -> Self {
        let (tx, rx) = channel();
        let app = LumiereEditApp::new(session).with_control(rx);
        let snapshots = std::env::var_os("FILMCRAFT_UI_SNAPSHOT_DIR").map(PathBuf::from);
        let mut b = Harness::builder().with_size(egui::vec2(1600.0, 980.0)).with_max_steps(10_000);
        if snapshots.is_some() {
            b = b.wgpu();
        }
        let harness = b.build_eframe(move |_cc| app);
        let mut d = Driver { harness, tx, snapshots };
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

    /// Run frames (with short sleeps so frame workers finish) until `done`.
    fn until(&mut self, what: &str, mut done: impl FnMut(&mut Self) -> bool) {
        for _ in 0..600 {
            if done(self) {
                return;
            }
            self.frames(1);
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
        panic!("timed out waiting for {what}");
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
        self.frames(3);
    }

    fn ids(&mut self, prefix: &str) -> Vec<String> {
        let v = self.ok("ui.elements", json!({"prefix": prefix}));
        v.as_array().unwrap().iter().filter_map(|e| e["id"].as_str().map(str::to_string)).collect()
    }

    fn app(&mut self) -> &mut LumiereEditApp {
        self.harness.state_mut()
    }

    /// Angles of the clips on V1 / A1 of the active sequence.
    fn angles(&mut self, audio: bool) -> Vec<u32> {
        let q = self.app().session.active_sequence().unwrap().clone();
        let t = if audio { &q.audio_tracks[0] } else { &q.video_tracks[0] };
        t.items.iter().map(|i| i.multicam.map_or(99, |m| m.angle)).collect()
    }

    fn snapshot(&mut self, name: &str) {
        let Some(dir) = self.snapshots.clone() else { return };
        self.frames(3);
        let img = match self.harness.render() {
            Ok(i) => i,
            Err(e) => {
                eprintln!("snapshot {name} skipped: {e}");
                return;
            }
        };
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join(format!("{name}.png"));
        img.save(&path).unwrap();
        eprintln!("snapshot: {}", path.display());
    }
}

#[test]
fn create_multicam_dialog_view_and_live_switching() {
    let (s, items) = session();
    let mut d = Driver::new(s);
    // Create Multi-Camera Source Sequence from the Project panel selection
    d.exec("project.select", json!({"items": items}));
    d.ok("ui.menu.invoke", json!({"id": "clip.createMulticam"}));
    d.frames(3);
    assert_eq!(d.app().ui.sync_dialog.as_ref().map(|x| x.kind.clone()).as_deref(), Some("multicam"));
    for id in ["mcam.name", "mcam.method.audio", "mcam.method.timecode", "mcam.audio.switch", "mcam.cameraNames.track", "mcam.processedBin", "mcam.ok"] {
        assert!(!d.ids(id).is_empty(), "{id}");
    }
    d.click("mcam.method.in");
    d.click("mcam.audio.switch");
    d.snapshot("multicam-create-dialog");
    d.click("mcam.ok");
    assert!(d.app().ui.sync_dialog.is_none(), "dialog closed: {}", d.app().ui.status);
    let src = d.app().session.state.project_selection[0];
    let mc = d.app().session.project.sequence(src).unwrap().multicam.clone().unwrap();
    assert_eq!(mc.cameras.len(), 4);
    assert_eq!(mc.cameras[0].name, "Cam A.mov");
    // edit it into the sequence, open the Multi-Camera view
    d.exec("source.open", json!({"item": src.0}));
    d.exec("playhead.set", json!({"frame": 0}));
    d.exec("source.overwrite", json!({}));
    d.exec("playhead.set", json!({"frame": 12}));
    d.ok("ui.menu.invoke", json!({"id": "multicam.toggleView"}));
    d.frames(2);
    assert!(d.app().ui.program.multicam);
    let angles = d.ids("program.multicam.angle.");
    assert_eq!(angles.len(), 4, "{angles:?}");
    d.until("grid frame", |d| {
        let k = d.app().frames.queue_len();
        k == 0
    });
    d.frames(6);
    d.snapshot("multicam-view");
    // clicking an angle switches the clip at the playhead; audio follows video when asked
    d.exec("multicam.audioFollowsVideo", json!({"enabled": true}));
    d.click("program.multicam.angle.2");
    assert_eq!(d.angles(false), [1]);
    assert_eq!(d.angles(true), [1], "audio followed");
    // Ctrl-click: video only
    d.ok("ui.click", json!({"id": "program.multicam.angle.3", "modifiers": {"ctrl": true}}));
    d.frames(3);
    assert_eq!((d.angles(false), d.angles(true)), (vec![2], vec![1]));
    d.until("program frame", |d| d.app().frames.queue_len() == 0);
    d.frames(6);
    d.snapshot("multicam-view-camera3");
    d.exec("playhead.set", json!({"frame": 0}));
    d.click("program.multicam.angle.1");
    // live switching: play, press 2 then 4, stop → one undo step with the recorded cuts
    let undo_before = d.app().session.history.undo.len();
    d.ok("ui.playback", json!({"action": "play"}));
    d.until("recording", |d| d.app().session.mcrec.active());
    d.until("playhead past 0.3 s", |d| d.app().session.playhead() > FrameRate::FPS_24.tick_of(7));
    d.ok("ui.key", json!({"key": "2"}));
    d.frames(2);
    d.until("playhead past 0.8 s", |d| d.app().session.playhead() > FrameRate::FPS_24.tick_of(19));
    d.ok("ui.key", json!({"key": "4"}));
    d.frames(4);
    d.snapshot("multicam-recording");
    d.until("playhead past 1.2 s", |d| d.app().session.playhead() > FrameRate::FPS_24.tick_of(29));
    d.ok("ui.playback", json!({"action": "stop"}));
    d.frames(2);
    assert!(!d.app().session.mcrec.active());
    let v = d.angles(false);
    assert_eq!(&v[..3], [0, 1, 3], "{v:?}");
    assert_eq!(d.angles(true)[..3], [0, 1, 3], "audio followed the recording");
    assert_eq!(d.app().session.history.undo.len(), undo_before + 1, "one undo step per pass");
    d.frames(4);
    d.snapshot("multicam-after-recording");
    d.exec("edit.undo", json!({}));
    assert_eq!(d.angles(false), [0]);
}

#[test]
fn synchronize_and_merge_dialogs() {
    let (s, items) = session();
    let mut d = Driver::new(s);
    // two cameras on V1 and V2 at different times
    d.exec("source.open", json!({"item": items[0]}));
    d.exec("playhead.set", json!({"frame": 0}));
    d.exec("source.overwrite", json!({}));
    d.exec("timeline.setTargeting", json!({"track": "V2", "sourcePatch": true}));
    d.exec("timeline.setTargeting", json!({"track": "A2", "sourcePatch": true}));
    d.exec("source.open", json!({"item": items[1]}));
    d.exec("playhead.set", json!({"frame": 30}));
    d.exec("source.overwrite", json!({}));
    assert_eq!(d.app().session.active_sequence().unwrap().video_tracks[1].items.len(), 1);
    let all: Vec<u64> = d.app().session.active_sequence().unwrap().all_tracks().flat_map(|t| t.items.iter().map(|i| i.id.0)).collect();
    d.exec("timeline.select", json!({"clips": all}));
    d.ok("ui.menu.invoke", json!({"id": "clip.synchronize"}));
    d.frames(3);
    assert_eq!(d.app().ui.sync_dialog.as_ref().map(|x| x.kind.clone()).as_deref(), Some("synchronize"));
    d.click("sync.method.in");
    d.snapshot("multicam-sync-dialog");
    d.click("sync.ok");
    assert!(d.app().ui.sync_dialog.is_none(), "{}", d.app().ui.status);
    let q = d.app().session.active_sequence().unwrap().clone();
    assert_eq!(q.video_tracks[0].items[0].start, q.video_tracks[1].items[0].start, "In points aligned");
    // Merge Clips needs audio clips: the dialog explains what is wrong
    d.exec("project.select", json!({"items": [items[0], items[1]]}));
    d.ok("ui.menu.invoke", json!({"id": "clip.mergeClips"}));
    d.frames(3);
    assert!(!d.ids("merge.removeVideoAudio").is_empty());
    d.snapshot("multicam-merge-dialog");
    d.click("merge.ok");
    let msg = d.app().ui.sync_dialog.as_ref().map(|x| x.message.clone()).unwrap_or_default();
    assert!(msg.contains("one video clip"), "{msg}");
    d.click("merge.cancel");
    assert!(d.app().ui.sync_dialog.is_none());
}
