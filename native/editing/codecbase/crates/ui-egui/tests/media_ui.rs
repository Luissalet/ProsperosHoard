//! Headless UI tests of media management: the Link Media dialog opening for a project whose media
//! moved (search a folder, link, the others follow), Project panel offline / proxy badges, the
//! monitors' Toggle Proxies button, Create Proxies and Project Manager dialogs.
//!
//! Set `FILMCRAFT_UI_SNAPSHOT_DIR=<dir>` to also render the window offscreen with wgpu and write PNGs
//! there (`media-*.png`).

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

fn tmp_dir(name: &str) -> PathBuf {
    let nanos = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos();
    let d = std::env::temp_dir().join(format!("lumiere-codecbase-ui-{name}-{}-{nanos}", std::process::id()));
    std::fs::create_dir_all(&d).unwrap();
    d
}

/// A 24 fps ProRes movie of a demo scene (our own encoder).
fn make_movie(path: &Path, scene: DemoScene, w: u32, h: u32, frames: i64) {
    let rate = FrameRate::FPS_24;
    let dur = rate.tick_of(frames);
    let src = GeneratorSource::new(Generator::Demo(scene), w, h, rate, dur);
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
    let seq = p.new_sequence("s", SequenceSettings { width: w, height: h, frame_rate: rate, ..Default::default() }, 1, 0, None);
    let mut v = p.make_track_item(item, TrackKind::Video, Tick::ZERO, TimeRange::new(Tick::ZERO, dur), rate).unwrap();
    for e in &mut v.effects {
        lumiere_codecbase_engine::project::resolve_auto_points(e, (w, h), (w, h));
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

/// `<root>/p.lumedit` with a sequence of `<root>/Media/{Harbour,Night}.mov`; the media folder is
/// then moved to `<root>/Moved/Media`.
fn moved_project(root: &Path) -> String {
    let media = root.join("Media");
    std::fs::create_dir_all(&media).unwrap();
    let files = [media.join("Harbour.mov"), media.join("Night.mov")];
    make_movie(&files[0], DemoScene::OceanSunset, 640, 360, 24);
    make_movie(&files[1], DemoScene::CityNight, 640, 360, 24);
    let mut s = Session::default();
    let r = s.execute("file.import", json!({"paths": files.iter().map(|f| f.to_string_lossy()).collect::<Vec<_>>()})).unwrap();
    let items: Vec<u64> = r["items"].as_array().unwrap().iter().map(|v| v.as_u64().unwrap()).collect();
    let rate = FrameRate::FPS_24;
    let mut p = (*s.project).clone();
    let seq = p.new_sequence("Harbour Cut", SequenceSettings { width: 640, height: 360, frame_rate: rate, ..Default::default() }, 1, 1, None);
    let mut at = Tick::ZERO;
    for it in items {
        let it = lumiere_codecbase_engine::project::ItemId(it);
        let d = rate.tick_of(24);
        let mut v = p.make_track_item(it, TrackKind::Video, at, TimeRange::new(Tick::ZERO, d), rate).unwrap();
        for e in &mut v.effects {
            lumiere_codecbase_engine::project::resolve_auto_points(e, (640, 360), (640, 360));
        }
        p.sequence_mut(seq).unwrap().video_tracks[0].items.push(v);
        at += d;
    }
    s.project = Arc::new(p);
    let path = root.join("p.lumedit").to_string_lossy().into_owned();
    s.execute("file.save", json!({"path": path})).unwrap();
    std::fs::create_dir_all(root.join("Moved")).unwrap();
    std::fs::rename(&media, root.join("Moved").join("Media")).unwrap();
    path
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
fn link_media_dialog_relinks_moved_media() {
    let root = tmp_dir("link");
    let path = moved_project(&root);
    let mut s = Session::default();
    s.execute("file.open", json!({"path": path})).unwrap();
    let mut d = Driver::new(s);
    d.frames(6);
    // the dialog opened by itself, listing both clips; the Project panel marks them
    let rows = d.ids("linkMedia.row.");
    assert_eq!(rows.len(), 2, "{rows:?}");
    assert!(!d.ids("linkMedia.locate").is_empty() && !d.ids("linkMedia.offlineAll").is_empty());
    d.snapshot("media-link-dialog");
    // search the moved folder for the selected clip
    let folder = root.join("Moved").to_string_lossy().into_owned();
    d.app().ui.link_media.as_mut().unwrap().folder = folder;
    d.click("linkMedia.search");
    assert_eq!(d.ids("linkMedia.candidate.").len(), 1);
    d.frames(4);
    d.snapshot("media-link-search");
    d.click("linkMedia.link");
    d.frames(4);
    assert!(d.app().ui.link_media.is_none(), "both clips relinked (the second by folder remap), dialog closed");
    let st = d.exec("media.status", json!({}));
    assert!(st.as_array().unwrap().iter().all(|m| m["status"] == "online"), "{st}");
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn offline_badges_proxy_toggle_and_dialogs() {
    let root = tmp_dir("badges");
    let path = moved_project(&root);
    let mut s = Session::default();
    s.execute("file.open", json!({"path": path})).unwrap();
    let mut d = Driver::new(s);
    d.frames(4);
    d.click("linkMedia.cancel");
    assert!(d.app().ui.link_media.is_none());
    // the Program monitor shows the offline slate; the project panel marks offline items
    d.exec("playhead.set", json!({"frame": 6}));
    d.frames(30);
    let offline_badges = d.ids("project.item.").into_iter().filter(|i| i.ends_with(".offline")).count();
    assert_eq!(offline_badges, 2, "icon-view offline markers");
    d.snapshot("media-offline-project");
    // relink by folder remap, create proxies through the dialog, toggle them from the monitor
    d.exec("media.autoRelink", json!({"from": root.join("Media").to_string_lossy(), "to": root.join("Moved/Media").to_string_lossy()}));
    let items: Vec<u64> = d.exec("media.status", json!({})).as_array().unwrap().iter().map(|m| m["item"].as_u64().unwrap()).collect();
    d.exec("project.select", json!({"items": items}));
    d.ok("ui.menu.invoke", json!({"id": "media.createProxies"}));
    d.frames(3);
    assert!(d.app().ui.create_proxies.is_some());
    d.snapshot("media-create-proxies");
    d.click("proxies.preset.prores_proxy_quarter");
    d.click("proxies.ok");
    for _ in 0..400 {
        d.frames(1);
        let attached = d.app().session.project.items.values().filter(|i| i.as_media().is_some_and(|m| m.proxy.is_some())).count();
        if attached == 2 {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(20));
    }
    assert_eq!(d.app().session.project.items.values().filter(|i| i.as_media().is_some_and(|m| m.proxy.is_some())).count(), 2, "proxies attached");
    d.click("program.transport.media.toggleProxies");
    assert!(d.app().session.prefs.media.enable_proxies);
    assert_eq!(d.ids("project.item.").into_iter().filter(|i| i.ends_with(".proxy")).count(), 2, "proxy badges");
    d.frames(20);
    d.snapshot("media-proxies-on");
    // Project Manager dialog: calculate the size estimate
    d.ok("ui.menu.invoke", json!({"id": "file.projectManager"}));
    d.frames(2);
    d.app().ui.project_manager.as_mut().unwrap().destination = root.join("Collected").to_string_lossy().into_owned();
    d.click("pm.calculate");
    let est = d.app().ui.project_manager.as_ref().unwrap().estimate;
    assert!(est.is_some_and(|(a, b, n)| a > 0 && b == a && n == 2), "{est:?}");
    d.snapshot("media-project-manager");
    d.click("pm.ok");
    assert!(d.app().ui.project_manager.is_none());
    let _ = std::fs::remove_dir_all(&root);
}
