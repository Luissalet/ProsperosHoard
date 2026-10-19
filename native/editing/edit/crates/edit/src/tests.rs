use super::*;
use lumiere_edit_project::{Label, Project, SequenceSettings, TrackKind};
use lumiere_edit_time::FrameRate;
use proptest::prelude::*;

const R: FrameRate = FrameRate::FPS_24;

fn f(n: i64) -> Tick {
    R.tick_of(n)
}

struct Fx {
    seq: Sequence,
    next: u64,
}

fn media(_: ItemId) -> Option<Tick> {
    Some(f(1000))
}

impl Fx {
    fn new() -> Self {
        let mut p = Project::new("t");
        let s = p.new_sequence("s", SequenceSettings { frame_rate: R, ..Default::default() }, 3, 2, None);
        let seq = p.sequence(s).unwrap().clone();
        Self { seq, next: 10_000 }
    }
    fn ctx<'a>(next: &'a mut u64) -> EditCtx<'a> {
        EditCtx { next_id: next, media_duration: &media, media_start: &|_| Tick::ZERO, min_duration: f(1) }
    }
    fn v(&self, i: usize) -> TrackId {
        self.seq.video_tracks[i].id
    }
    fn a(&self, i: usize) -> TrackId {
        self.seq.audio_tracks[i].id
    }
    fn item(&mut self, start: i64, dur: i64, src_in: i64) -> TrackItem {
        self.next += 1;
        TrackItem {
            id: ClipId(self.next),
            item: ItemId(1),
            name: format!("c{}", self.next),
            label: Label::Iris,
            start: f(start),
            duration: f(dur),
            source_in: f(src_in),
            speed: 1.0,
            reverse: false,
            enabled: true,
            link: None,
            group: None,
            effects: vec![],
            markers: vec![],
            gain_db: 0.0,
            frame_hold: None,
            scale_to_frame: false,
            essential: None,
            multicam: None,
            time_interpolation: Default::default(),
            hold_filters: false,
            field_options: None,
            source_channels: Vec::new(),
            graphic: None,
        }
    }
    fn put(&mut self, track: TrackId, start: i64, dur: i64, src_in: i64) -> ClipId {
        let it = self.item(start, dur, src_in);
        let id = it.id;
        let mut n = self.next;
        overwrite(&mut self.seq, vec![(track, it)], &mut Self::ctx(&mut n)).unwrap();
        self.next = n;
        id
    }
    fn spans(&self, track: TrackId) -> Vec<(i64, i64)> {
        self.seq.track(track).unwrap().items.iter().map(|i| (R.frame_at(i.start), R.frame_at(i.duration))).collect()
    }
}

#[test]
fn overwrite_splits_underlying() {
    let mut fx = Fx::new();
    let v1 = fx.v(0);
    fx.put(v1, 0, 100, 0);
    fx.put(v1, 40, 20, 500);
    assert_eq!(fx.spans(v1), vec![(0, 40), (40, 20), (60, 40)]);
    let right = &fx.seq.track(v1).unwrap().items[2];
    assert_eq!(right.source_in, f(60), "right remainder keeps media continuity");
    fx.seq.check().unwrap();
}

#[test]
fn insert_ripples_sync_locked_tracks() {
    let mut fx = Fx::new();
    let (v1, v2, a1) = (fx.v(0), fx.v(1), fx.a(0));
    fx.put(v1, 0, 50, 0);
    fx.put(v2, 60, 10, 0);
    fx.put(a1, 0, 100, 0);
    fx.seq.track_mut(v2).unwrap().sync_lock = false;
    let it = fx.item(20, 10, 0);
    let mut n = fx.next;
    insert(&mut fx.seq, vec![(v1, it)], &mut Fx::ctx(&mut n)).unwrap();
    fx.next = n;
    assert_eq!(fx.spans(v1), vec![(0, 20), (20, 10), (30, 30)]);
    assert_eq!(fx.spans(a1), vec![(0, 20), (30, 80)], "sync-locked audio split and shifted");
    assert_eq!(fx.spans(v2), vec![(60, 10)], "not sync-locked: untouched");
}

#[test]
fn razor_keeps_content_continuous() {
    let mut fx = Fx::new();
    let v1 = fx.v(0);
    let c = fx.put(v1, 10, 100, 200);
    let before = fx.seq.find_item(c).unwrap().1.source_time_at(f(70));
    let mut n = fx.next;
    let new = razor(&mut fx.seq, &[], f(50), &mut Fx::ctx(&mut n));
    assert_eq!(new.len(), 1);
    let right = fx.seq.find_item(new[0]).unwrap().1;
    assert_eq!(right.source_time_at(f(70)), before);
    assert_eq!(fx.spans(v1), vec![(10, 40), (50, 60)]);
}

#[test]
fn extract_and_lift() {
    let mut fx = Fx::new();
    let (v1, a1) = (fx.v(0), fx.a(0));
    fx.put(v1, 0, 100, 0);
    fx.put(a1, 0, 100, 0);
    let mut n = fx.next;
    lift(&mut fx.seq, &[v1], TimeRange::new(f(10), f(10)), &mut Fx::ctx(&mut n));
    assert_eq!(fx.spans(v1), vec![(0, 10), (20, 80)]);
    extract(&mut fx.seq, &[v1], TimeRange::new(f(30), f(10)), &mut Fx::ctx(&mut n));
    assert_eq!(fx.spans(v1), vec![(0, 10), (20, 10), (30, 60)]);
    assert_eq!(fx.spans(a1), vec![(0, 30), (30, 60)]);
}

#[test]
fn ripple_delete_and_conflict() {
    let mut fx = Fx::new();
    let (v1, a1) = (fx.v(0), fx.a(0));
    let a = fx.put(v1, 0, 10, 0);
    fx.put(v1, 10, 10, 0);
    ripple_delete_items(&mut fx.seq, &[a]).unwrap();
    assert_eq!(fx.spans(v1), vec![(0, 10)]);
    // audio under the gap blocks the ripple
    let b = fx.put(v1, 20, 10, 0);
    fx.put(a1, 22, 3, 0);
    assert_eq!(ripple_delete_items(&mut fx.seq, &[b]), Err(EditError::SyncLockConflict));
    assert_eq!(fx.spans(v1), vec![(0, 10), (20, 10)], "unchanged on failure");
}

#[test]
fn close_gap_works() {
    let mut fx = Fx::new();
    let v1 = fx.v(0);
    fx.put(v1, 0, 10, 0);
    fx.put(v1, 25, 10, 0);
    close_gap(&mut fx.seq, v1, f(15)).unwrap();
    assert_eq!(fx.spans(v1), vec![(0, 10), (10, 10)]);
}

#[test]
fn regular_trim_respects_neighbours_and_handles() {
    let mut fx = Fx::new();
    let v1 = fx.v(0);
    let a = fx.put(v1, 0, 10, 5);
    fx.put(v1, 12, 10, 0);
    let mut n = fx.next;
    // out: only 2 frames of space before the neighbour
    let d = trim(&mut fx.seq, a, Edge::Out, TrimMode::Regular, f(5), &mut Fx::ctx(&mut n)).unwrap();
    assert_eq!(d, f(2));
    // in: 5 frames of head handle, but item starts at 0 so no space to the left
    let d = trim(&mut fx.seq, a, Edge::In, TrimMode::Regular, -f(3), &mut Fx::ctx(&mut n)).unwrap();
    assert_eq!(d, Tick::ZERO);
    let d = trim(&mut fx.seq, a, Edge::In, TrimMode::Regular, f(3), &mut Fx::ctx(&mut n)).unwrap();
    assert_eq!(d, f(3));
    assert_eq!(fx.seq.find_item(a).unwrap().1.source_in, f(8));
    fx.seq.check().unwrap();
}

#[test]
fn ripple_trim_shifts_following() {
    let mut fx = Fx::new();
    let (v1, a1) = (fx.v(0), fx.a(0));
    let a = fx.put(v1, 0, 10, 0);
    fx.put(v1, 10, 10, 0);
    fx.put(a1, 30, 5, 0);
    let mut n = fx.next;
    trim(&mut fx.seq, a, Edge::Out, TrimMode::Ripple, f(4), &mut Fx::ctx(&mut n)).unwrap();
    assert_eq!(fx.spans(v1), vec![(0, 14), (14, 10)]);
    assert_eq!(fx.spans(a1), vec![(34, 5)]);
    trim(&mut fx.seq, a, Edge::In, TrimMode::Ripple, f(2), &mut Fx::ctx(&mut n)).unwrap();
    assert_eq!(fx.spans(v1), vec![(0, 12), (12, 10)]);
}

#[test]
fn roll_slip_slide() {
    let mut fx = Fx::new();
    let v1 = fx.v(0);
    let a = fx.put(v1, 0, 10, 10);
    let b = fx.put(v1, 10, 10, 10);
    let c = fx.put(v1, 20, 10, 10);
    let mut n = fx.next;
    roll(&mut fx.seq, a, b, f(3), &mut Fx::ctx(&mut n)).unwrap();
    assert_eq!(fx.spans(v1), vec![(0, 13), (13, 7), (20, 10)]);
    assert_eq!(fx.seq.find_item(b).unwrap().1.source_in, f(13));
    let d = slip(&mut fx.seq, c, -f(50), &mut Fx::ctx(&mut n)).unwrap();
    assert_eq!(d, -f(10), "slip clamps at media start");
    slide(&mut fx.seq, b, f(2), &mut Fx::ctx(&mut n)).unwrap();
    assert_eq!(fx.spans(v1), vec![(0, 15), (15, 7), (22, 8)]);
    fx.seq.check().unwrap();
}

#[test]
fn transitions_stay_on_their_cuts() {
    let mut fx = Fx::new();
    let v1 = fx.v(0);
    let a = fx.put(v1, 0, 10, 10);
    let b = fx.put(v1, 10, 10, 10);
    let c = fx.put(v1, 20, 10, 10);
    let cross = |id, start, from, to| Transition {
        id: TransitionId(id),
        effect: lumiere_edit_project::find_effect("cross_dissolve").unwrap().instance(),
        start: f(start),
        duration: f(4),
        from,
        to,
        align: Default::default(),
        reverse: false,
    };
    let t = fx.seq.track_mut(v1).unwrap();
    t.transitions = vec![cross(1, 8, Some(a), Some(b)), cross(2, 18, Some(b), Some(c)), cross(3, 26, Some(c), None)];
    // every transition is centred on its cut (or ends with the clip it fades out)
    let check = |fx: &Fx, what: &str| {
        let t = fx.seq.track(v1).unwrap();
        for tr in &t.transitions {
            let cut = match tr.to {
                Some(to) => t.item(to).unwrap().start,
                None => t.item(tr.from.unwrap()).unwrap().end() - f(2),
            };
            assert_eq!(tr.start + f(2), cut, "{what}: transition {:?} left its cut", tr.id);
        }
        assert_eq!(t.transitions.len(), 3, "{what}");
    };
    let mut n = fx.next;
    trim(&mut fx.seq, a, Edge::Out, TrimMode::Ripple, f(4), &mut Fx::ctx(&mut n)).unwrap();
    check(&fx, "ripple trim out");
    trim(&mut fx.seq, b, Edge::In, TrimMode::Ripple, f(3), &mut Fx::ctx(&mut n)).unwrap();
    check(&fx, "ripple trim in");
    ripple_trim_group(&mut fx.seq, &[b], Edge::Out, -f(2), &mut Fx::ctx(&mut n)).unwrap();
    check(&fx, "ripple trim group");
    roll(&mut fx.seq, b, c, f(2), &mut Fx::ctx(&mut n)).unwrap();
    check(&fx, "roll");
    slide(&mut fx.seq, b, -f(3), &mut Fx::ctx(&mut n)).unwrap();
    check(&fx, "slide");
    set_speed(&mut fx.seq, b, 0.5, false, true, &mut Fx::ctx(&mut n)).unwrap();
    check(&fx, "ripple speed change");
    fx.seq.check().unwrap();
}

#[test]
fn rate_stretch_and_speed() {
    let mut fx = Fx::new();
    let v1 = fx.v(0);
    let a = fx.put(v1, 0, 10, 0);
    let mut n = fx.next;
    let s = rate_stretch(&mut fx.seq, a, Edge::Out, f(10), &mut Fx::ctx(&mut n)).unwrap();
    assert!((s - 0.5).abs() < 1e-9);
    set_speed(&mut fx.seq, a, 2.0, false, true, &mut Fx::ctx(&mut n)).unwrap();
    assert_eq!(fx.spans(v1), vec![(0, 5)]);
}

#[test]
fn move_with_insert_and_overwrite() {
    let mut fx = Fx::new();
    let (v1, v2) = (fx.v(0), fx.v(1));
    let a = fx.put(v1, 0, 10, 0);
    fx.put(v2, 0, 30, 0);
    let mut n = fx.next;
    move_items(&mut fx.seq, &[(a, v2, f(5))], false, &mut Fx::ctx(&mut n)).unwrap();
    assert_eq!(fx.spans(v1), vec![]);
    assert_eq!(fx.spans(v2), vec![(0, 5), (5, 10), (15, 15)]);
}

#[derive(Debug, Clone)]
enum Op {
    Over(usize, i64, i64),
    Ins(usize, i64, i64),
    Razor(i64),
    Extract(i64, i64),
    TrimOut(usize, i64),
    Ripple(usize),
}

fn op() -> impl Strategy<Value = Op> {
    prop_oneof![
        (0usize..3, 0i64..200, 1i64..50).prop_map(|(t, s, d)| Op::Over(t, s, d)),
        (0usize..3, 0i64..200, 1i64..50).prop_map(|(t, s, d)| Op::Ins(t, s, d)),
        (0i64..250).prop_map(Op::Razor),
        (0i64..200, 1i64..30).prop_map(|(s, d)| Op::Extract(s, d)),
        (0usize..20, -20i64..20).prop_map(|(i, d)| Op::TrimOut(i, d)),
        (0usize..20).prop_map(Op::Ripple),
    ]
}

proptest! {
    #[test]
    fn never_overlaps(ops in proptest::collection::vec(op(), 1..40)) {
        let mut fx = Fx::new();
        let tracks = [fx.v(0), fx.v(1), fx.a(0)];
        for o in ops {
            let mut n = fx.next;
            match o {
                Op::Over(t, s, d) => { let it = fx.item(s, d, 0); n = fx.next; let _ = overwrite(&mut fx.seq, vec![(tracks[t], it)], &mut Fx::ctx(&mut n)); }
                Op::Ins(t, s, d) => { let it = fx.item(s, d, 0); n = fx.next; let _ = insert(&mut fx.seq, vec![(tracks[t], it)], &mut Fx::ctx(&mut n)); }
                Op::Razor(t) => { razor(&mut fx.seq, &[], f(t), &mut Fx::ctx(&mut n)); }
                Op::Extract(s, d) => { extract(&mut fx.seq, &[tracks[0]], TimeRange::new(f(s), f(d)), &mut Fx::ctx(&mut n)); }
                Op::TrimOut(i, d) => {
                    let ids: Vec<ClipId> = fx.seq.all_tracks().flat_map(|t| t.items.iter().map(|x| x.id)).collect();
                    if let Some(c) = ids.get(i) { let _ = trim(&mut fx.seq, *c, Edge::Out, TrimMode::Regular, f(d), &mut Fx::ctx(&mut n)); }
                }
                Op::Ripple(i) => {
                    let ids: Vec<ClipId> = fx.seq.all_tracks().flat_map(|t| t.items.iter().map(|x| x.id)).collect();
                    if let Some(c) = ids.get(i) { let _ = ripple_delete_items(&mut fx.seq, &[*c]); }
                }
            }
            fx.next = fx.next.max(n);
            prop_assert!(fx.seq.check().is_ok(), "{:?}", fx.seq.check());
            for t in fx.seq.all_tracks() { for i in &t.items { prop_assert!(i.start >= Tick::ZERO); } }
        }
    }

    #[test]
    fn insert_then_extract_is_identity(s in 0i64..100, d in 1i64..40) {
        let mut fx = Fx::new();
        let (v1, a1) = (fx.v(0), fx.a(0));
        fx.put(v1, 0, 60, 0);
        fx.put(v1, 70, 30, 100);
        fx.put(a1, 10, 80, 0);
        let before: Vec<Vec<(i64, i64)>> = [v1, a1].iter().map(|t| fx.spans(*t)).collect();
        let it = fx.item(s, d, 0);
        let mut n = fx.next;
        insert(&mut fx.seq, vec![(v1, it)], &mut Fx::ctx(&mut n)).unwrap();
        extract(&mut fx.seq, &[v1], TimeRange::new(f(s), f(d)), &mut Fx::ctx(&mut n));
        // content ranges are identical after merging split pieces
        for (k, t) in [v1, a1].iter().enumerate() {
            let merged = merge(fx.spans(*t));
            prop_assert_eq!(merged, merge(before[k].clone()));
        }
    }
}

fn merge(v: Vec<(i64, i64)>) -> Vec<(i64, i64)> {
    let mut out: Vec<(i64, i64)> = Vec::new();
    for (s, d) in v {
        if let Some(last) = out.last_mut()
            && last.0 + last.1 == s
        {
            last.1 += d;
            continue;
        }
        out.push((s, d));
    }
    out
}

#[test]
fn unused_kind_import() {
    let _ = TrackKind::Video;
}
