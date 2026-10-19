//! Structural checks of written AAF files against the required properties and rules of the AAF
//! Object Specification v1.1 and the Edit Protocol, read back through our compound file reader.

use std::collections::HashSet;

use lumiere_codecbase_project::{Project, SequenceSettings, TrackKind, TransitionAlign, TransitionId, find_effect};
use lumiere_codecbase_time::{FrameRate, TimeRange};

use super::ids::{self, cls, ddef, def, path, pid};
use super::store::{self, Obj, Value};
use super::*;

fn project() -> (Project, ItemId) {
    let mut p = Project::new("t");
    let info = lumiere_codecbase_media::MediaInfo {
        name: "a.mov".into(),
        kind: lumiere_codecbase_media::MediaKind::Movie,
        duration: FrameRate::FPS_24.tick_of(2400),
        video: Some(lumiere_codecbase_media::VideoStreamInfo {
            width: 1280,
            height: 720,
            frame_rate: FrameRate::FPS_24,
            par: (1, 1),
            codec: String::new(),
            pixel_format: String::new(),
            color: Default::default(),
            has_alpha: false,
            bitrate: None,
        }),
        audio: Some(lumiere_codecbase_media::AudioStreamInfo { sample_rate: 48_000, channels: 2, codec: String::new(), bits_per_sample: Some(24) }),
        container: String::new(),
        start_timecode: Some(86_400),
        file_size: None,
    };
    let a = p.add_item(
        "a.mov",
        lumiere_codecbase_project::Label::Iris,
        lumiere_codecbase_project::ItemKind::Media(lumiere_codecbase_project::MediaClip {
            media: lumiere_codecbase_project::MediaRef::File { path: "/m/a.mov".into() },
            info,
            interpret: Default::default(),
            mark_in: None,
            mark_out: None,
            markers: vec![],
            offline: false,
            proxy: None,
            identity: None,
        }),
        None,
    );
    let r = FrameRate::FPS_24;
    let s = p.new_sequence("S", SequenceSettings { frame_rate: r, ..Default::default() }, 1, 1, None);
    let mut ids = Vec::new();
    for (kind, start, dur, src) in
        [(TrackKind::Video, 0, 48, 10), (TrackKind::Video, 48, 48, 200), (TrackKind::Audio, 0, 48, 10), (TrackKind::Audio, 48, 48, 200)]
    {
        let ti = p.make_track_item(a, kind, r.tick_of(start), TimeRange::new(r.tick_of(src), r.tick_of(dur)), r).unwrap();
        ids.push(ti.id);
        let q = p.sequence_mut(s).unwrap();
        q.tracks_mut(kind)[0].items.push(ti);
    }
    for (kind, i, eff) in [(TrackKind::Video, 0, "cross_dissolve"), (TrackKind::Audio, 2, "constant_power")] {
        let id = TransitionId(p.alloc_id());
        let q = p.sequence_mut(s).unwrap();
        q.tracks_mut(kind)[0].transitions.push(lumiere_codecbase_project::Transition {
            id,
            effect: find_effect(eff).unwrap().instance(),
            start: r.tick_of(42),
            duration: r.tick_of(12),
            from: Some(ids[i]),
            to: Some(ids[i + 1]),
            align: TransitionAlign::CenterAtCut,
            reverse: false,
        });
    }
    let q = p.sequence_mut(s).unwrap();
    q.audio_tracks[0].items[1].gain_db = -3.0;
    (p, s)
}

fn walk<'a>(o: &'a Obj, out: &mut Vec<&'a Obj>) {
    out.push(o);
    for (_, v) in &o.props {
        match v {
            Value::Strong(c) => walk(c, out),
            Value::StrongVec(v) | Value::StrongSet(v, _) => v.iter().for_each(|c| walk(c, out)),
            _ => {}
        }
    }
}

fn keys(dict: &Obj, p: u16) -> HashSet<Vec<u8>> {
    dict.objs(p).iter().filter_map(|d| d.data(pid::IDENTIFICATION).map(<[u8]>::to_vec)).collect()
}

#[test]
fn written_files_have_the_required_structure() {
    let (p, s) = project();
    for small in [false, true] {
        let (bytes, _) = export(&p, s, &AafOptions { small_sectors: small, ..Default::default() }).unwrap();
        // compound file level
        let cf = lumiere_codecbase_cfb::CompoundFile::open(&bytes).unwrap();
        assert_eq!(cf.version, if small { 3 } else { 4 });
        assert_eq!(cf.root().clsid, ids::ROOT);
        assert!(cf.read_path("referenced properties").is_ok());
        let h = cf.find("Header-2").unwrap();
        assert_eq!(cf.entries[h].clsid, cls::HEADER);
        assert!(cf.read_path("Header-2/properties").unwrap().starts_with(&[0x4C, 0x20]));

        // object level
        let root = store::read(&bytes).unwrap();
        assert!(root.strong(pid::ROOT_META_DICTIONARY).is_some());
        let header = root.strong(pid::ROOT_HEADER).unwrap();
        for req in [pid::BYTE_ORDER, pid::LAST_MODIFIED, pid::CONTENT, pid::DICTIONARY, pid::VERSION, pid::IDENTIFICATION_LIST, pid::OBJECT_MODEL_VERSION] {
            assert!(header.get(req).is_some(), "Header property {req:#06x}");
        }
        assert_eq!(header.auid(pid::OPERATIONAL_PATTERN), Some(def::OP_EDIT_PROTOCOL));
        let ident = &header.objs(pid::IDENTIFICATION_LIST)[0];
        for req in [pid::COMPANY_NAME, pid::PRODUCT_NAME, pid::PRODUCT_VERSION_STRING, pid::PRODUCT_ID, pid::DATE, pid::GENERATION_AUID] {
            assert!(ident.get(req).is_some(), "Identification property {req:#06x}");
        }
        let dict = header.strong(pid::DICTIONARY).unwrap();
        let ddefs = keys(dict, pid::DATA_DEFINITIONS);
        let opdefs = keys(dict, pid::OPERATION_DEFINITIONS);
        let idefs = keys(dict, pid::INTERPOLATION_DEFINITIONS);
        let cdefs = keys(dict, pid::CONTAINER_DEFINITIONS);
        let pdefs = keys(dict, pid::PARAMETER_DEFINITIONS);
        let content = header.strong(pid::CONTENT).unwrap();
        let mobs = content.objs(pid::MOBS);
        let mob_ids: Vec<[u8; 32]> = mobs.iter().map(|m| m.mob_id(pid::MOB_ID).unwrap()).collect();
        assert_eq!(mob_ids.iter().collect::<HashSet<_>>().len(), mob_ids.len(), "unique mob ids");
        assert!(matches!(content.get(pid::MOBS), Some(Value::StrongSet(_, k)) if *k == pid::MOB_ID));
        let comps: Vec<&Obj> = mobs.iter().filter(|m| m.class == cls::COMPOSITION_MOB).collect();
        assert_eq!(comps.len(), 1);
        assert_eq!(comps[0].auid(pid::USAGE_CODE), Some(def::USAGE_TOP_LEVEL));
        assert_eq!(mobs.iter().filter(|m| m.class == cls::MASTER_MOB).count(), 1);
        assert_eq!(mobs.iter().filter(|m| m.class == cls::SOURCE_MOB).count(), 3, "picture file, sound file, tape");
        for m in mobs {
            for req in [pid::MOB_ID, pid::MOB_NAME, pid::SLOTS, pid::MOB_LAST_MODIFIED, pid::MOB_CREATION_TIME] {
                assert!(m.get(req).is_some(), "Mob property {req:#06x}");
            }
            if m.class == cls::SOURCE_MOB {
                let d = m.strong(pid::ESSENCE_DESCRIPTION).expect("SourceMob::EssenceDescription");
                if d.class != cls::TAPE_DESCRIPTOR {
                    for req in [pid::SAMPLE_RATE, pid::FILE_LENGTH, pid::CONTAINER_FORMAT] {
                        assert!(d.get(req).is_some(), "FileDescriptor property {req:#06x}");
                    }
                    assert!(cdefs.contains(d.weak_key(pid::CONTAINER_FORMAT).unwrap()));
                    let url = d.objs(pid::LOCATOR)[0].string(pid::URL_STRING).unwrap();
                    assert_eq!(url, "file:///m/a.mov");
                }
                if d.class == cls::PCM_DESCRIPTOR {
                    assert_eq!(d.u32(pid::CHANNELS), Some(2));
                    assert_eq!(d.u32(pid::QUANTIZATION_BITS), Some(24));
                    assert_eq!(d.u16(pid::BLOCK_ALIGN), Some(6));
                }
            }
            for slot in m.objs(pid::SLOTS) {
                assert!(slot.get(pid::SLOT_ID).is_some() && slot.get(pid::SEGMENT).is_some());
                if slot.class == cls::TIMELINE_MOB_SLOT {
                    assert!(slot.rational(pid::EDIT_RATE).is_some_and(|r| r.0 > 0 && r.1 > 0));
                    assert!(slot.get(pid::ORIGIN).is_some());
                }
            }
            let mut all = Vec::new();
            walk(m, &mut all);
            for o in all {
                let is_component = [cls::FILLER, cls::SOURCE_CLIP, cls::SEQUENCE, cls::TRANSITION, cls::OPERATION_GROUP, cls::TIMECODE, cls::COMMENT_MARKER]
                    .contains(&o.class);
                if is_component {
                    let dd = o.weak_key(pid::DATA_DEFINITION).expect("Component::DataDefinition");
                    assert!(ddefs.contains(dd), "data definition in the dictionary");
                    let event_seq = o.class == cls::SEQUENCE && dd == ddef::DESCRIPTIVE_METADATA;
                    assert!(event_seq || o.get(pid::LENGTH).is_some(), "Component::Length");
                }
                if o.class == cls::SOURCE_CLIP {
                    let id = o.mob_id(pid::SOURCE_ID).unwrap();
                    let slot = o.u32(pid::SOURCE_MOB_SLOT_ID).unwrap();
                    assert!(o.get(pid::START_TIME).is_some());
                    if id != [0; 32] {
                        let t = mobs.iter().find(|m| m.mob_id(pid::MOB_ID) == Some(id)).expect("source clip target in the file");
                        assert!(t.objs(pid::SLOTS).iter().any(|s| s.u32(pid::SLOT_ID) == Some(slot)), "target slot exists");
                    }
                }
                if o.class == cls::OPERATION_GROUP {
                    assert!(opdefs.contains(o.weak_key(pid::OPERATION).unwrap()));
                    for prm in o.objs(pid::PARAMETERS) {
                        assert!(pdefs.contains(prm.auid(pid::PARAMETER_DEFINITION).unwrap().as_slice()));
                        if prm.class == cls::VARYING_VALUE {
                            assert!(idefs.contains(prm.weak_key(pid::INTERPOLATION).unwrap()));
                            assert!(!prm.objs(pid::POINT_LIST).is_empty());
                        }
                    }
                }
                if o.class == cls::SEQUENCE && o.get(pid::LENGTH).is_some() {
                    let cs = o.objs(pid::COMPONENTS);
                    let mut sum = 0i64;
                    for (i, c) in cs.iter().enumerate() {
                        let l = c.i64(pid::LENGTH).unwrap();
                        if c.class == cls::TRANSITION {
                            // Edit Protocol: a transition sits between two segments at least as long
                            assert!(i > 0 && i + 1 < cs.len(), "transition at a sequence end");
                            assert!(cs[i - 1].class != cls::TRANSITION && cs[i + 1].class != cls::TRANSITION);
                            assert!(cs[i - 1].i64(pid::LENGTH).unwrap() >= l && cs[i + 1].i64(pid::LENGTH).unwrap() >= l);
                            let cut = c.i64(pid::CUT_POINT).unwrap();
                            assert!((0..=l).contains(&cut));
                            assert!(c.strong(pid::OPERATION_GROUP).is_some());
                            sum -= l;
                        } else {
                            sum += l;
                        }
                    }
                    assert_eq!(sum, o.i64(pid::LENGTH).unwrap(), "sequence length = segments − transitions");
                }
            }
        }
        // the composition: timecode + V1 + A1 slots, the video dissolve and the gain
        let comp = comps[0];
        let slots = comp.objs(pid::SLOTS);
        let tc = slots[0].strong(pid::SEGMENT).unwrap();
        assert_eq!(tc.class, cls::TIMECODE);
        assert_eq!(tc.u16(pid::TC_FPS), Some(24));
        let v1 = slots[1].strong(pid::SEGMENT).unwrap().objs(pid::COMPONENTS);
        assert_eq!(v1.iter().map(|c| c.class).collect::<Vec<_>>(), vec![cls::SOURCE_CLIP, cls::TRANSITION, cls::SOURCE_CLIP]);
        assert_eq!(v1[0].i64(pid::LENGTH), Some(54), "outgoing clip extends to the transition end");
        assert_eq!(v1[2].i64(pid::LENGTH), Some(54), "incoming clip starts at the transition start");
        assert_eq!(v1[2].i64(pid::START_TIME), Some(194));
        assert_eq!(v1[1].i64(pid::CUT_POINT), Some(6));
        assert_eq!(v1[1].strong(pid::OPERATION_GROUP).unwrap().weak_key(pid::OPERATION), Some(&def::VIDEO_DISSOLVE[..]));
        let a1 = slots[2].strong(pid::SEGMENT).unwrap().objs(pid::COMPONENTS);
        assert_eq!(slots[2].rational(pid::EDIT_RATE), Some((48_000, 1)));
        assert_eq!(a1[2].class, cls::OPERATION_GROUP);
        assert_eq!(a1[2].weak_key(pid::OPERATION), Some(&def::MONO_AUDIO_GAIN[..]));
        let amp = a1[2].objs(pid::PARAMETERS)[0].data(pid::CONSTANT_VALUE).unwrap();
        let (n, d) = store::rational_of(&amp[17..]).unwrap();
        assert!((n as f64 / d as f64 - 10f64.powf(-3.0 / 20.0)).abs() < 1e-4);
        assert_eq!(a1[1].strong(pid::OPERATION_GROUP).unwrap().weak_key(pid::OPERATION), Some(&def::MONO_AUDIO_DISSOLVE[..]));
        // weak reference paths are registered
        let rp = cf.read_path("referenced properties").unwrap();
        let pids: Vec<u16> = rp[7..].chunks_exact(2).map(|c| u16::from_le_bytes([c[0], c[1]])).collect();
        assert!(pids.windows(3).any(|w| w == path::DATA_DEFS));
    }
}

#[test]
fn store_round_trips_every_stored_form() {
    let leaf = |n: u8| Obj::new([n; 16]).data_prop(0x1B01, vec![n; 16]);
    let root = Obj::new(ids::ROOT)
        .data_prop(0x0003, vec![1, 2, 3])
        .with(0x0004, Value::Strong(Box::new(leaf(1).with(0x0005, Value::StrongVec(vec![leaf(2), leaf(3)])))))
        .with(
            0x0002,
            Value::Strong(Box::new(
                Obj::new([9; 16]).with(0x3B04, Value::Strong(Box::new(Obj::new([8; 16]).with(0x2605, Value::StrongSet(vec![leaf(4), leaf(5)], 0x1B01))))),
            )),
        )
        .with(0x0006, Value::Weak(path::DATA_DEFS.to_vec(), 0x1B01, vec![5; 16]))
        .with(0x0007, Value::WeakVec(path::DATA_DEFS.to_vec(), 0x1B01, vec![vec![4; 16], vec![5; 16]]))
        .with(0x0008, Value::Stream((0..10_000u32).map(|i| i as u8).collect()));
    for v in [lumiere_codecbase_cfb::Version::V3, lumiere_codecbase_cfb::Version::V4] {
        let bytes = store::write(&root, v).unwrap();
        assert_eq!(store::read(&bytes).unwrap(), root);
    }
    // a data value longer than a property can hold is an error, not a panic
    assert!(store::write(&Obj::new(ids::ROOT).data_prop(1, vec![0; 70_000]), lumiere_codecbase_cfb::Version::V4).is_err());
}
