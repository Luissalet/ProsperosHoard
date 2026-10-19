//! Timeline edit algebra.
//!
//! Every function takes a `&mut Sequence` (the engine passes a copy-on-write clone) and either
//! applies the whole edit or returns an error leaving the sequence untouched (functions validate
//! first, or work on a clone). Track items never overlap afterwards (checked in tests).
//!
//! Semantics follow Premiere Pro:
//! - **Overwrite** replaces whatever is under the new item on its track.
//! - **Insert** pushes material right on the target tracks *and every sync-locked, unlocked track*.
//! - **Lift** leaves a gap; **Extract** closes it (ripple) on targeted + sync-locked tracks.
//! - **Ripple delete** removes items and closes the gap; fails if a sync-locked track has material in
//!   the gap (it would lose sync).
//! - Trims: regular, ripple, roll, slip, slide and rate stretch, all limited by media handles and
//!   neighbouring items.
//!
//! Keyframes are stored in media time, so trims and splits never need to move them.

#![cfg_attr(not(test), deny(clippy::unwrap_used, clippy::expect_used, clippy::panic, clippy::unimplemented, clippy::todo, clippy::unreachable))]

pub mod captions;
pub mod multicam;
pub mod through;
pub mod transcript;

use std::collections::HashMap;

use lumiere_edit_project::{ClipId, ItemId, Sequence, Track, TrackId, TrackItem, Transition, TransitionId};
use lumiere_edit_time::{Tick, TimeRange};

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum EditError {
    #[error("no such track item {0:?}")]
    NoItem(ClipId),
    #[error("no such track {0:?}")]
    NoTrack(TrackId),
    #[error("track is locked")]
    Locked,
    #[error("this edit would break sync on a sync-locked track")]
    SyncLockConflict,
    #[error("not enough media (handles) for this trim")]
    NoHandles,
    #[error("edit would make a clip shorter than one frame")]
    TooShort,
    #[error("nothing to do")]
    Nothing,
    #[error("{0}")]
    Other(String),
}

pub type Result<T> = std::result::Result<T, EditError>;

/// Supplies fresh ids and media durations to edits.
pub struct EditCtx<'a> {
    pub next_id: &'a mut u64,
    /// Media duration of a project item (None = unlimited, e.g. stills / adjustment layers): the
    /// latest media time its clips may show.
    pub media_duration: &'a dyn Fn(ItemId) -> Option<Tick>,
    /// The earliest media time clips of a project item may show (a subclip that restricts trims
    /// starts at its In point; everything else at 0).
    pub media_start: &'a dyn Fn(ItemId) -> Tick,
    /// Minimum item duration (one sequence frame).
    pub min_duration: Tick,
}

impl EditCtx<'_> {
    pub fn alloc(&mut self) -> u64 {
        let v = *self.next_id;
        *self.next_id += 1;
        v
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Edge {
    In,
    Out,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TrimMode {
    Regular,
    Ripple,
}

// ---------------------------------------------------------------------------------------------
// Track-level primitives
// ---------------------------------------------------------------------------------------------

/// Split the item strictly containing `t` into two. Returns the new (right) item's id.
/// `links` maps old link groups to the new group for right-hand pieces (so linked partners split
/// in the same operation stay linked to each other).
pub fn split_track_at(track: &mut Track, t: Tick, ctx: &mut EditCtx, links: &mut HashMap<u64, u64>) -> Option<ClipId> {
    let idx = track.items.iter().position(|i| i.start < t && t < i.end())?;
    let left = &mut track.items[idx];
    let mut right = left.clone();
    let cut = t - left.start;
    left.duration = cut;
    let new_id = ClipId(ctx.alloc());
    right.id = new_id;
    right.start = t;
    right.duration -= cut;
    if !right.reverse && right.frame_hold.is_none() {
        right.source_in += Tick((cut.0 as f64 * right.speed.abs()).round() as i64);
    } else if right.reverse {
        // reversed: the left piece now shows the later media; keep content continuous
        let left_ref = &mut track.items[idx];
        let consumed = Tick((cut.0 as f64 * left_ref.speed.abs()).round() as i64);
        let total_src = Tick(((cut + right.duration).0 as f64 * left_ref.speed.abs()).round() as i64);
        right.source_in = left_ref.source_in;
        left_ref.source_in += total_src - consumed;
    }
    if let Some(l) = right.link {
        let nl = *links.entry(l).or_insert_with(|| ctx.alloc());
        right.link = Some(nl);
    }
    // transitions: those after the cut that referenced the left piece now reference the right one;
    // a transition spanning the cut on this clip's interior is removed.
    let old_id = track.items[idx].id;
    track
        .transitions
        .retain(|tr| !(tr.start < t && t < tr.end() && (tr.from == Some(old_id) || tr.to == Some(old_id)) && !(tr.from.is_some() && tr.to.is_some())));
    for tr in &mut track.transitions {
        if tr.start >= t && tr.from == Some(old_id) {
            tr.from = Some(new_id);
        }
    }
    track.items.insert(idx + 1, right);
    Some(new_id)
}

/// Remove all material in `range` on a track (splitting items at the boundaries).
pub fn clear_track_range(track: &mut Track, range: TimeRange, ctx: &mut EditCtx, links: &mut HashMap<u64, u64>) -> Vec<ClipId> {
    if range.is_empty() {
        return Vec::new();
    }
    split_track_at(track, range.start, ctx, links);
    split_track_at(track, range.end(), ctx, links);
    let mut removed = Vec::new();
    track.items.retain(|i| {
        let inside = i.start >= range.start && i.end() <= range.end();
        if inside {
            removed.push(i.id);
        }
        !inside
    });
    remove_orphan_transitions(track);
    removed
}

/// Shift every item starting at or after `at` by `delta`.
pub fn shift_track_from(track: &mut Track, at: Tick, delta: Tick) {
    for i in &mut track.items {
        if i.start >= at {
            i.start += delta;
        }
    }
    for tr in &mut track.transitions {
        if tr.start >= at || (tr.end() > at && tr.from.is_none()) {
            tr.start += delta;
        }
    }
    track.sort();
}

/// Open a gap of `dur` at `at` (splitting an item that spans `at`).
pub fn insert_track_gap(track: &mut Track, at: Tick, dur: Tick, ctx: &mut EditCtx, links: &mut HashMap<u64, u64>) {
    split_track_at(track, at, ctx, links);
    shift_track_from(track, at, dur);
}

/// Whether `range` holds no items on a track.
pub fn track_range_empty(track: &Track, range: TimeRange) -> bool {
    !track.items.iter().any(|i| i.range().overlaps(&range))
}

/// Drop transitions whose clips no longer exist or are no longer adjacent to them.
pub fn remove_orphan_transitions(track: &mut Track) {
    let ids: HashMap<ClipId, (Tick, Tick)> = track.items.iter().map(|i| (i.id, (i.start, i.end()))).collect();
    track.transitions.retain(|tr| {
        let from_ok = tr.from.is_none_or(|f| ids.contains_key(&f));
        let to_ok = tr.to.is_none_or(|t| ids.contains_key(&t));
        from_ok && to_ok && (tr.from.is_some() || tr.to.is_some())
    });
}

/// Where a transition sits: the incoming clip's start, or the outgoing clip's end for a fade to nothing.
fn transition_anchor(track: &Track, tr: &Transition) -> Option<Tick> {
    match (tr.from, tr.to) {
        (_, Some(to)) => track.item(to).map(|i| i.start),
        (Some(from), None) => track.item(from).map(|i| i.end()),
        (None, None) => None,
    }
}

/// Keep transitions on their edit points after an edit moved cuts (ripple trim, roll, slide, ripple
/// speed change): each transition shifts by as much as its anchor clip edge moved.
fn transitions_follow_cuts(before: &Sequence, after: &mut Sequence) {
    for track in after.all_tracks_mut() {
        let Some(old) = before.track(track.id) else { continue };
        let shifts: Vec<Tick> = track
            .transitions
            .iter()
            .map(|tr| match (transition_anchor(old, tr), transition_anchor(track, tr)) {
                (Some(a), Some(b)) => b - a,
                _ => Tick::ZERO,
            })
            .collect();
        for (tr, d) in track.transitions.iter_mut().zip(shifts) {
            tr.start += d;
        }
    }
}

fn place(track: &mut Track, item: TrackItem) {
    let idx = track.items.partition_point(|i| i.start <= item.start);
    track.items.insert(idx, item);
}

// ---------------------------------------------------------------------------------------------
// Sequence edits
// ---------------------------------------------------------------------------------------------

fn track_mut(seq: &mut Sequence, id: TrackId) -> Result<&mut Track> {
    seq.track_mut(id).ok_or(EditError::NoTrack(id))
}

/// Overwrite edits: each item replaces material on its track.
pub fn overwrite(seq: &mut Sequence, placements: Vec<(TrackId, TrackItem)>, ctx: &mut EditCtx) -> Result<Vec<ClipId>> {
    let mut links = HashMap::new();
    let mut ids = Vec::new();
    for (tid, _) in &placements {
        if track_mut(seq, *tid)?.locked {
            return Err(EditError::Locked);
        }
    }
    for (tid, item) in placements {
        let t = track_mut(seq, tid)?;
        clear_track_range(t, item.range(), ctx, &mut links);
        ids.push(item.id);
        place(t, item);
    }
    Ok(ids)
}

/// Insert edits: open a gap on target tracks and all sync-locked unlocked tracks, then place.
pub fn insert(seq: &mut Sequence, placements: Vec<(TrackId, TrackItem)>, ctx: &mut EditCtx) -> Result<Vec<ClipId>> {
    if placements.is_empty() {
        return Err(EditError::Nothing);
    }
    for (tid, _) in &placements {
        if track_mut(seq, *tid)?.locked {
            return Err(EditError::Locked);
        }
    }
    let at = placements.iter().map(|p| p.1.start).min().unwrap_or_default();
    let dur = placements.iter().map(|p| p.1.end()).max().unwrap_or_default() - at;
    let targets: Vec<TrackId> = placements.iter().map(|p| p.0).collect();
    let mut links = HashMap::new();
    for t in seq.all_tracks_mut() {
        if !t.locked && (targets.contains(&t.id) || t.sync_lock) {
            insert_track_gap(t, at, dur, ctx, &mut links);
        }
    }
    for ct in seq.caption_tracks.iter_mut().filter(|c| !c.locked && c.sync_lock) {
        captions::insert_gap(ct, at, dur, ctx);
    }
    let mut ids = Vec::new();
    for (tid, item) in placements {
        let t = track_mut(seq, tid)?;
        ids.push(item.id);
        place(t, item);
    }
    Ok(ids)
}

/// Add Edit (razor) at `t` on the given tracks (all unlocked tracks when empty). Returns new ids.
pub fn razor(seq: &mut Sequence, tracks: &[TrackId], t: Tick, ctx: &mut EditCtx) -> Vec<ClipId> {
    let mut links = HashMap::new();
    let mut out = Vec::new();
    for tr in seq.all_tracks_mut() {
        if tr.locked || (!tracks.is_empty() && !tracks.contains(&tr.id)) {
            continue;
        }
        if let Some(id) = split_track_at(tr, t, ctx, &mut links) {
            out.push(id);
        }
    }
    out
}

/// Razor only the given items (and their linked partners if listed) at `t`.
pub fn razor_items(seq: &mut Sequence, items: &[ClipId], t: Tick, ctx: &mut EditCtx) -> Vec<ClipId> {
    let tracks: Vec<TrackId> = items.iter().filter_map(|c| seq.find_item(*c).map(|(tid, _)| tid)).collect();
    let mut links = HashMap::new();
    let mut out = Vec::new();
    for tr in seq.all_tracks_mut() {
        if tr.locked || !tracks.contains(&tr.id) {
            continue;
        }
        // only split if the item at t is one of ours
        if tr.item_at(t).is_some_and(|i| items.contains(&i.id))
            && let Some(id) = split_track_at(tr, t, ctx, &mut links)
        {
            out.push(id);
        }
    }
    out
}

/// Lift: remove `range` on tracks, leaving a gap.
pub fn lift(seq: &mut Sequence, tracks: &[TrackId], range: TimeRange, ctx: &mut EditCtx) -> Vec<ClipId> {
    let mut links = HashMap::new();
    let mut removed = Vec::new();
    for tr in seq.all_tracks_mut() {
        if !tr.locked && tracks.contains(&tr.id) {
            removed.extend(clear_track_range(tr, range, ctx, &mut links));
        }
    }
    removed
}

/// Extract: remove `range` on targeted and sync-locked tracks and close the gap.
pub fn extract(seq: &mut Sequence, tracks: &[TrackId], range: TimeRange, ctx: &mut EditCtx) -> Vec<ClipId> {
    let mut links = HashMap::new();
    let mut removed = Vec::new();
    for tr in seq.all_tracks_mut() {
        if tr.locked || !(tracks.contains(&tr.id) || tr.sync_lock) {
            continue;
        }
        removed.extend(clear_track_range(tr, range, ctx, &mut links));
        shift_track_from(tr, range.end(), -range.duration);
    }
    for ct in seq.caption_tracks.iter_mut().filter(|c| !c.locked && c.sync_lock) {
        captions::extract_range(ct, range);
    }
    removed
}

/// Delete track items (leaving gaps).
pub fn delete_items(seq: &mut Sequence, items: &[ClipId]) -> usize {
    let mut n = 0;
    for tr in seq.all_tracks_mut() {
        if tr.locked {
            continue;
        }
        let before = tr.items.len();
        tr.items.retain(|i| !items.contains(&i.id));
        n += before - tr.items.len();
        remove_orphan_transitions(tr);
    }
    n
}

/// Ripple delete track items: remove them and close the resulting gaps.
pub fn ripple_delete_items(seq: &mut Sequence, items: &[ClipId]) -> Result<()> {
    // Collect per-track ranges to close (merged), processed right-to-left.
    let mut ranges: Vec<(TrackId, TimeRange)> = Vec::new();
    for c in items {
        let (tid, it) = seq.find_item(*c).ok_or(EditError::NoItem(*c))?;
        ranges.push((tid, it.range()));
    }
    // Group identical ranges across tracks (linked A/V) into one ripple.
    let mut spans: Vec<TimeRange> = ranges.iter().map(|r| r.1).collect();
    spans.sort_by_key(|r| (r.start, r.duration));
    spans.dedup();
    let mut work = seq.clone();
    delete_items(&mut work, items);
    let affected: Vec<TrackId> = ranges.iter().map(|r| r.0).collect();
    for span in spans.iter().rev() {
        // the gap actually closable: from span.start to the next material on affected tracks
        for tr in work.all_tracks_mut() {
            if tr.locked {
                continue;
            }
            let on_affected = affected.contains(&tr.id);
            if !on_affected && !tr.sync_lock {
                continue;
            }
            if !on_affected && !track_range_empty(tr, *span) {
                return Err(EditError::SyncLockConflict);
            }
            shift_track_from(tr, span.end(), -span.duration);
        }
    }
    *seq = work;
    Ok(())
}

/// Close the gap containing `t` on a track (Ripple Delete on a gap).
pub fn close_gap(seq: &mut Sequence, track: TrackId, t: Tick) -> Result<()> {
    let tr = seq.track(track).ok_or(EditError::NoTrack(track))?;
    if tr.item_at(t).is_some() {
        return Err(EditError::Nothing);
    }
    let prev_end = tr.items.iter().filter(|i| i.end() <= t).map(|i| i.end()).max().unwrap_or(Tick::ZERO);
    let next_start = tr.items.iter().filter(|i| i.start > t).map(|i| i.start).min().ok_or(EditError::Nothing)?;
    let gap = TimeRange::from_bounds(prev_end, next_start);
    let mut work = seq.clone();
    for tr in work.all_tracks_mut() {
        if tr.locked {
            continue;
        }
        if tr.id != track {
            if !tr.sync_lock {
                continue;
            }
            if !track_range_empty(tr, gap) {
                return Err(EditError::SyncLockConflict);
            }
        }
        shift_track_from(tr, gap.end(), -gap.duration);
    }
    *seq = work;
    Ok(())
}

/// Move items by (track, time) offsets with overwrite (default drag) or insert (Cmd-drag) semantics.
/// `moves`: (item, destination track, new start).
pub fn move_items(seq: &mut Sequence, moves: &[(ClipId, TrackId, Tick)], insert_mode: bool, ctx: &mut EditCtx) -> Result<()> {
    let mut work = seq.clone();
    let mut placed = Vec::new();
    for (c, dest, start) in moves {
        let (_, it) = work.find_item(*c).ok_or(EditError::NoItem(*c))?;
        let mut it = it.clone();
        if work.track(*dest).ok_or(EditError::NoTrack(*dest))?.locked {
            return Err(EditError::Locked);
        }
        it.start = (*start).max(Tick::ZERO);
        placed.push((*dest, it));
    }
    // carry transitions with moved clips? Premiere drops transitions whose partner is not moved.
    delete_items(&mut work, &moves.iter().map(|m| m.0).collect::<Vec<_>>());
    if insert_mode {
        insert(&mut work, placed, ctx)?;
    } else {
        overwrite(&mut work, placed, ctx)?;
    }
    *seq = work;
    Ok(())
}

fn media_len(ctx: &EditCtx, item: &TrackItem) -> Option<Tick> {
    if item.frame_hold.is_some() {
        return None;
    }
    (ctx.media_duration)(item.item)
}

/// Media available before a clip's source In (its head handle, media ticks).
fn head(ctx: &EditCtx, item: &TrackItem) -> Tick {
    if item.frame_hold.is_some() {
        return item.source_in;
    }
    (item.source_in - (ctx.media_start)(item.item)).max(Tick::ZERO)
}

fn src_of(dur: Tick, speed: f64) -> Tick {
    Tick((dur.0 as f64 * speed.abs()).round() as i64)
}

/// Neighbours on the same track: (previous end, next start).
fn neighbours(track: &Track, id: ClipId) -> (Tick, Tick) {
    let idx = track.items.iter().position(|i| i.id == id).unwrap_or(0);
    let prev_end = if idx > 0 { track.items[idx - 1].end() } else { Tick::ZERO };
    let next_start = track.items.get(idx + 1).map(|i| i.start).unwrap_or(Tick::MAX);
    (prev_end, next_start)
}

/// Compute the clamped delta a trim may apply (for UI feedback while dragging).
pub fn clamp_trim(seq: &Sequence, clip: ClipId, edge: Edge, mode: TrimMode, delta: Tick, ctx: &EditCtx) -> Result<Tick> {
    let (tid, it) = seq.find_item(clip).ok_or(EditError::NoItem(clip))?;
    let tr = seq.track(tid).ok_or(EditError::NoTrack(tid))?;
    let (prev_end, next_start) = neighbours(tr, clip);
    let media = media_len(ctx, it);
    let speed = it.speed.abs().max(1e-9);
    let mut d = delta;
    match edge {
        Edge::In => {
            // extending left (d<0) needs media before source_in; shortening needs min duration
            let max_ext = Tick((head(ctx, it).0 as f64 / speed).floor() as i64);
            let lo = if mode == TrimMode::Regular { (-(it.start - prev_end)).max(-max_ext) } else { -max_ext };
            let hi = it.duration - ctx.min_duration;
            d = d.clamp(if it.frame_hold.is_some() { Tick::MIN } else { lo }, hi);
        }
        Edge::Out => {
            let lo = -(it.duration - ctx.min_duration);
            let mut hi = if mode == TrimMode::Regular { next_start - it.end() } else { Tick::MAX };
            if let Some(m) = media {
                let remain = Tick(((m - it.source_out()).0 as f64 / speed).floor() as i64);
                hi = hi.min(remain);
            }
            d = d.clamp(lo, hi.max(lo));
        }
    }
    Ok(d)
}

/// Trim one edge of an item (Selection-tool edge drag = Regular; Ripple tool = Ripple).
/// Linked partners should be trimmed by the caller with the same delta.
pub fn trim(seq: &mut Sequence, clip: ClipId, edge: Edge, mode: TrimMode, delta: Tick, ctx: &mut EditCtx) -> Result<Tick> {
    let d = clamp_trim(seq, clip, edge, mode, delta, ctx)?;
    if d == Tick::ZERO {
        return Ok(d);
    }
    let (tid, it) = seq.find_item(clip).ok_or(EditError::NoItem(clip))?;
    let old_end = it.end();
    let old_start = it.start;
    let speed = it.speed.abs();
    let mut work = seq.clone();
    {
        let (_, it) = work.find_item_mut(clip).ok_or(EditError::NoItem(clip))?;
        match edge {
            Edge::In => {
                if !it.reverse && it.frame_hold.is_none() {
                    it.source_in += src_of(d, speed);
                }
                it.duration -= d;
                if mode == TrimMode::Regular {
                    it.start += d;
                }
            }
            Edge::Out => {
                if it.reverse {
                    it.source_in -= src_of(d, speed);
                }
                it.duration += d;
            }
        }
    }
    if mode == TrimMode::Ripple {
        let (at, shift) = match edge {
            Edge::In => (old_start + Tick(1), -d),
            Edge::Out => (old_end, d),
        };
        for tr in work.all_tracks_mut() {
            if tr.locked {
                continue;
            }
            if tr.id == tid || tr.sync_lock {
                if tr.id != tid && shift < Tick::ZERO && !track_range_empty(tr, TimeRange::new(at + shift - Tick(if edge == Edge::In { 1 } else { 0 }), -shift))
                {
                    return Err(EditError::SyncLockConflict);
                }
                let from = if edge == Edge::In { old_start + Tick(1) } else { old_end };
                for i in &mut tr.items {
                    if i.id != clip && i.start >= from {
                        i.start += shift;
                    }
                }
                tr.sort();
            }
        }
        transitions_follow_cuts(seq, &mut work);
    }
    work.check().map_err(EditError::Other)?;
    *seq = work;
    Ok(d)
}

/// Ripple-trim one edge of a group of linked items (e.g. a clip and its audio partners) as a single
/// edit: every member's edge moves by the same delta, later material on the members' tracks and on
/// sync-locked tracks shifts by the change. Only tracks without a group member can block the edit
/// (a sync-locked track whose material would be overwritten).
pub fn ripple_trim_group(seq: &mut Sequence, clips: &[ClipId], edge: Edge, delta: Tick, ctx: &mut EditCtx) -> Result<Tick> {
    let Some(&first) = clips.first() else { return Ok(Tick::ZERO) };
    let mut d = delta;
    for c in clips {
        let x = clamp_trim(seq, *c, edge, TrimMode::Ripple, d, ctx)?;
        if x.abs() < d.abs() {
            d = x;
        }
    }
    if d == Tick::ZERO {
        return Ok(d);
    }
    let mut work = seq.clone();
    // track → shift origin for the members on it
    let mut origins: Vec<(TrackId, Tick)> = Vec::new();
    for c in clips {
        let (tid, it) = work.find_item_mut(*c).ok_or(EditError::NoItem(*c))?;
        let speed = it.speed.abs();
        let from = match edge {
            Edge::In => it.start + Tick(1),
            Edge::Out => it.end(),
        };
        match edge {
            Edge::In => {
                if !it.reverse && it.frame_hold.is_none() {
                    it.source_in += src_of(d, speed);
                }
                it.duration -= d;
            }
            Edge::Out => {
                if it.reverse {
                    it.source_in -= src_of(d, speed);
                }
                it.duration += d;
            }
        }
        if !origins.iter().any(|(t, _)| *t == tid) {
            origins.push((tid, from));
        }
    }
    let main_from = {
        let (tid, _) = seq.find_item(first).ok_or(EditError::NoItem(first))?;
        origins.iter().find(|(t, _)| *t == tid).map(|o| o.1).unwrap_or_default()
    };
    let shift = if edge == Edge::In { -d } else { d };
    for tr in work.all_tracks_mut() {
        if tr.locked {
            continue;
        }
        let member = origins.iter().find(|(t, _)| *t == tr.id).map(|o| o.1);
        if member.is_none() && !tr.sync_lock {
            continue;
        }
        let from = member.unwrap_or(main_from);
        if member.is_none() && shift < Tick::ZERO {
            let at = if edge == Edge::In { from - Tick(1) } else { from };
            if !track_range_empty(tr, TimeRange::new(at + shift, -shift)) {
                return Err(EditError::SyncLockConflict);
            }
        }
        for i in &mut tr.items {
            if !clips.contains(&i.id) && i.start >= from {
                i.start += shift;
            }
        }
        tr.sort();
    }
    transitions_follow_cuts(seq, &mut work);
    work.check().map_err(EditError::Other)?;
    *seq = work;
    Ok(d)
}

/// Rolling edit between two adjacent items on one track: moves the cut by `delta`.
pub fn roll(seq: &mut Sequence, left: ClipId, right: ClipId, delta: Tick, ctx: &mut EditCtx) -> Result<Tick> {
    let (_, l) = seq.find_item(left).ok_or(EditError::NoItem(left))?;
    let (_, r) = seq.find_item(right).ok_or(EditError::NoItem(right))?;
    if l.end() != r.start {
        return Err(EditError::Other("items are not adjacent".into()));
    }
    let mut d = delta;
    // left out-point limits
    d = d.max(-(l.duration - ctx.min_duration)).min(r.duration - ctx.min_duration);
    if let Some(m) = media_len(ctx, l) {
        d = d.min(Tick(((m - l.source_out()).0 as f64 / l.speed.abs()).floor() as i64));
    }
    d = d.max(-Tick((head(ctx, r).0 as f64 / r.speed.abs()).floor() as i64));
    if d == Tick::ZERO {
        return Ok(d);
    }
    let (ls, rs) = (l.speed.abs(), r.speed.abs());
    let before = seq.clone();
    {
        let (_, l) = seq.find_item_mut(left).ok_or(EditError::NoItem(left))?;
        l.duration += d;
    }
    {
        let (_, r) = seq.find_item_mut(right).ok_or(EditError::NoItem(right))?;
        r.start += d;
        r.duration -= d;
        r.source_in += src_of(d, rs);
    }
    let _ = ls;
    transitions_follow_cuts(&before, seq);
    Ok(d)
}

/// Slip: change which part of the media an item shows without moving it.
pub fn slip(seq: &mut Sequence, clip: ClipId, delta_media: Tick, ctx: &mut EditCtx) -> Result<Tick> {
    let (_, it) = seq.find_item(clip).ok_or(EditError::NoItem(clip))?;
    let used = src_of(it.duration, it.speed);
    let max_in = media_len(ctx, it).map(|m| m - used).unwrap_or(Tick::MAX);
    let lo = if it.frame_hold.is_some() { Tick::ZERO } else { (ctx.media_start)(it.item) };
    let new_in = (it.source_in + delta_media).clamp(lo, max_in.max(lo));
    let d = new_in - it.source_in;
    let (_, it) = seq.find_item_mut(clip).ok_or(EditError::NoItem(clip))?;
    it.source_in = new_in;
    Ok(d)
}

/// Slide: move an item between its neighbours, trimming them to keep the gapless span.
pub fn slide(seq: &mut Sequence, clip: ClipId, delta: Tick, ctx: &mut EditCtx) -> Result<Tick> {
    let (tid, it) = seq.find_item(clip).ok_or(EditError::NoItem(clip))?;
    let it = it.clone();
    let tr = seq.track(tid).ok_or(EditError::NoTrack(tid))?;
    let idx = tr.items.iter().position(|i| i.id == clip).ok_or(EditError::NoItem(clip))?;
    let prev = idx.checked_sub(1).map(|i| tr.items[i].clone()).filter(|p| p.end() == it.start);
    let next = tr.items.get(idx + 1).cloned().filter(|n| n.start == it.end());
    let mut d = delta;
    if let Some(p) = &prev {
        d = d.max(-(p.duration - ctx.min_duration));
        if let Some(m) = media_len(ctx, p) {
            d = d.min(Tick(((m - p.source_out()).0 as f64 / p.speed.abs()).floor() as i64));
        }
    } else {
        d = d.max(-(it.start - neighbours(tr, clip).0));
    }
    if let Some(n) = &next {
        d = d.min(n.duration - ctx.min_duration);
        d = d.max(-Tick((head(ctx, n).0 as f64 / n.speed.abs()).floor() as i64));
    } else {
        d = d.min(neighbours(tr, clip).1 - it.end());
    }
    if d == Tick::ZERO {
        return Ok(d);
    }
    let before = seq.clone();
    let t = seq.track_mut(tid).ok_or(EditError::NoTrack(tid))?;
    if let Some(p) = prev {
        let pi = t.item_mut(p.id).ok_or(EditError::NoItem(p.id))?;
        pi.duration += d;
    }
    if let Some(n) = next {
        let ni = t.item_mut(n.id).ok_or(EditError::NoItem(n.id))?;
        ni.start += d;
        ni.duration -= d;
        ni.source_in += src_of(d, n.speed);
    }
    let me = t.item_mut(clip).ok_or(EditError::NoItem(clip))?;
    me.start += d;
    transitions_follow_cuts(&before, seq);
    Ok(d)
}

/// Rate stretch: change an item's duration by dragging an edge, adjusting speed to keep the same media.
pub fn rate_stretch(seq: &mut Sequence, clip: ClipId, edge: Edge, delta: Tick, ctx: &mut EditCtx) -> Result<f64> {
    let (tid, it) = seq.find_item(clip).ok_or(EditError::NoItem(clip))?;
    let tr = seq.track(tid).ok_or(EditError::NoTrack(tid))?;
    let (prev_end, next_start) = neighbours(tr, clip);
    let src_len = it.source_out() - it.source_in;
    let d = match edge {
        Edge::Out => delta.clamp(-(it.duration - ctx.min_duration), next_start - it.end()),
        Edge::In => delta.clamp(-(it.start - prev_end), it.duration - ctx.min_duration),
    };
    let new_dur = match edge {
        Edge::Out => it.duration + d,
        Edge::In => it.duration - d,
    };
    let speed = src_len.0 as f64 / new_dur.0 as f64;
    let (_, it) = seq.find_item_mut(clip).ok_or(EditError::NoItem(clip))?;
    if edge == Edge::In {
        it.start += d;
    }
    it.duration = new_dur;
    it.speed = speed;
    Ok(speed)
}

/// Set speed/duration (Clip ▸ Speed/Duration…). `ripple` shifts following material.
pub fn set_speed(seq: &mut Sequence, clip: ClipId, speed: f64, reverse: bool, ripple: bool, ctx: &mut EditCtx) -> Result<()> {
    if speed <= 0.0 {
        return Err(EditError::Other("speed must be positive".into()));
    }
    let (tid, it) = seq.find_item(clip).ok_or(EditError::NoItem(clip))?;
    let src_len = it.source_out() - it.source_in;
    let mut new_dur = Tick((src_len.0 as f64 / speed).round() as i64).max(ctx.min_duration);
    let old_end = it.end();
    let tr = seq.track(tid).ok_or(EditError::NoTrack(tid))?;
    let (_, next_start) = neighbours(tr, clip);
    if !ripple {
        new_dur = new_dur.min(next_start - it.start);
    }
    let delta = new_dur - it.duration;
    let before = ripple.then(|| seq.clone());
    {
        let (_, it) = seq.find_item_mut(clip).ok_or(EditError::NoItem(clip))?;
        it.speed = speed;
        it.reverse = reverse;
        it.duration = new_dur;
    }
    if ripple && delta != Tick::ZERO {
        for tr in seq.all_tracks_mut() {
            if !tr.locked && (tr.id == tid || tr.sync_lock) {
                for i in &mut tr.items {
                    if i.id != clip && i.start >= old_end {
                        i.start += delta;
                    }
                }
                tr.sort();
            }
        }
    }
    if let Some(before) = before {
        transitions_follow_cuts(&before, seq);
    }
    Ok(())
}

/// Add a transition at the cut between `from` and `to` (or at a single clip edge).
pub fn add_transition(seq: &mut Sequence, track: TrackId, mut tr: Transition, ctx: &mut EditCtx) -> Result<TransitionId> {
    let t = seq.track_mut(track).ok_or(EditError::NoTrack(track))?;
    if t.locked {
        return Err(EditError::Locked);
    }
    tr.id = TransitionId(ctx.alloc());
    // Replace an existing transition at the same place.
    t.transitions.retain(|x| !(x.range().overlaps(&tr.range()) && (x.from == tr.from || x.to == tr.to)));
    let id = tr.id;
    t.transitions.push(tr);
    t.sort();
    Ok(id)
}

#[cfg(test)]
mod tests;
