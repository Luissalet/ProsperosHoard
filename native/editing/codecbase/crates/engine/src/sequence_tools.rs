//! Sequence and Markers menu commands: gaps, reverse match frame, subsequences, track deletion,
//! through edits, split edit points, range and chapter markers and the marker toggles.
//!
//! | Id | Menu | Default key |
//! |---|---|---|
//! | `sequence.reverseMatchFrame` | Sequence ▸ Reverse Match Frame | Shift+R |
//! | `sequence.goToNextGap` / `sequence.goToPrevGap` | Sequence ▸ Go to Gap ▸ Next / Previous in Sequence | Shift+; / Cmd+Shift+; |
//! | `sequence.goToNextGapInTrack` / `sequence.goToPrevGapInTrack` | Sequence ▸ Go to Gap ▸ Next / Previous in Track | |
//! | `sequence.selectionFollowsPlayhead` | Sequence ▸ Selection Follows Playhead | |
//! | `sequence.showThroughEdits` | Sequence ▸ Show Through Edits | |
//! | `sequence.joinThroughEdits` | (clip context menu) Join Through Edits | |
//! | `sequence.throughEdits` | query: the through edits of the active sequence | |
//! | `sequence.makeSubsequence` | Sequence ▸ Make Subsequence | Shift+U |
//! | `sequence.deleteTracks` | Sequence ▸ Delete Tracks… | |
//! | `markers.markSplitVideoIn` … `markers.markSplitAudioOut` | Markers ▸ Mark Split ▸ Video In … Audio Out | |
//! | `markers.goToSplitVideoIn` … `markers.goToSplitAudioOut` | Markers ▸ Go to Split ▸ Video In … Audio Out | |
//! | `markers.addRange` | Markers ▸ Add Range Marker | Ctrl+Shift+M |
//! | `markers.addRangeInOut` | Markers ▸ Add Range Marker to In and Out | Ctrl+M |
//! | `markers.showAllMarkerColors` / `markers.filterColors` | Markers ▸ Show All Marker Colors / Markers panel colour filter | |
//! | `markers.addChapter` | Markers ▸ Add Chapter Marker… | |
//! | `markers.rippleSequenceMarkers` | Markers ▸ Ripple Sequence Markers | |
//! | `markers.copyPasteIncludesSequenceMarkers` | Markers ▸ Copy Paste Includes Sequence Markers | |
//!
//! Split points: Mark Split sets a video-only or audio-only In/Out (on the sequence, or with
//! `"target":"source"` on the Source monitor clip). Ordinary Mark In / Mark Out clear the split of
//! that side. Insert / Overwrite from the Source monitor honour source split points: each channel
//! takes its own range and lands offset by the difference of the In points (J- and L-cuts).

use lumiere_codecbase_edit as edit;
use lumiere_codecbase_project::{ClipId, ItemId, Label, Marker, MarkerId, MarkerKind, SplitMarks, TrackId, TrackItem, TrackKind};
use lumiere_codecbase_time::{Tick, TimeRange};
use serde_json::{Value, json};

use crate::commands::{CommandSpec, always, bad, bool_p, clips_p, has_seq, str_p, time_p, track_p, with_links};
use crate::{EngineError, Result, Session};

type Run = fn(&mut Session, &Value) -> Result<Value>;
type Enabled = fn(&Session) -> std::result::Result<(), String>;

fn spec(
    id: &'static str,
    label: &'static str,
    menu: &'static [&'static str],
    shortcut: Option<&'static str>,
    params: &'static str,
    enabled: Enabled,
    run: Run,
) -> CommandSpec {
    CommandSpec { id, label, menu, shortcut, params, enabled, run, journal: true }
}

fn query(id: &'static str, label: &'static str, params: &'static str, run: Run) -> CommandSpec {
    CommandSpec { id, label, menu: &[], shortcut: None, params, enabled: always, run, journal: false }
}

/// Insert this module's commands into the registry after their anchors, so the menus list them in
/// Premiere's order.
pub(crate) fn splice(v: &mut Vec<CommandSpec>) {
    for (anchor, specs) in groups() {
        let at = v.iter().position(|c| c.id == anchor).map(|i| i + 1).unwrap_or(v.len());
        for (k, c) in specs.into_iter().enumerate() {
            v.insert(at + k, c);
        }
    }
}

const SPLIT: &str = r#"{"time":ticks?,"target":"program|source"}"#;

fn groups() -> Vec<(&'static str, Vec<CommandSpec>)> {
    vec![
        (
            "sequence.matchFrame",
            vec![spec("sequence.reverseMatchFrame", "Reverse Match Frame", &["Sequence"], Some("Shift+R"), "{}", has_source, |s, _| reverse_match_frame(s))],
        ),
        (
            "sequence.closeGap",
            vec![
                spec("sequence.goToNextGap", "Next in Sequence", &["Sequence", "Go to Gap"], Some("Shift+;"), "{}", has_seq, |s, p| {
                    go_to_gap(s, p, true, false)
                }),
                spec("sequence.goToPrevGap", "Previous in Sequence", &["Sequence", "Go to Gap"], Some("Cmd+Shift+;"), "{}", has_seq, |s, p| {
                    go_to_gap(s, p, false, false)
                }),
                spec("sequence.goToNextGapInTrack", "Next in Track", &["Sequence", "Go to Gap"], None, r#"{"track":"V1"|id?}"#, has_seq, |s, p| {
                    go_to_gap(s, p, true, true)
                }),
                spec("sequence.goToPrevGapInTrack", "Previous in Track", &["Sequence", "Go to Gap"], None, r#"{"track":"V1"|id?}"#, has_seq, |s, p| {
                    go_to_gap(s, p, false, true)
                }),
            ],
        ),
        (
            "sequence.linkedSelection",
            vec![
                spec("sequence.selectionFollowsPlayhead", "Selection Follows Playhead", &["Sequence"], None, r#"{"on":bool?}"#, always, |s, p| {
                    s.state.selection_follows_playhead = bool_p(p, "on").unwrap_or(!s.state.selection_follows_playhead);
                    if s.state.selection_follows_playhead {
                        select_under_playhead(s);
                    }
                    Ok(json!({"selectionFollowsPlayhead": s.state.selection_follows_playhead}))
                }),
                spec("sequence.showThroughEdits", "Show Through Edits", &["Sequence"], None, r#"{"on":bool?}"#, always, |s, p| {
                    s.state.show_through_edits = bool_p(p, "on").unwrap_or(!s.state.show_through_edits);
                    Ok(json!({"showThroughEdits": s.state.show_through_edits}))
                }),
                spec("sequence.joinThroughEdits", "Join Through Edits", &[], None, r#"{"clips":[id]?,"all":bool?}"#, has_seq, join_through_edits),
                query("sequence.throughEdits", "List Through Edits", "{}", |s, _| {
                    let q = s.active_sequence().ok_or(EngineError::NoSequence)?;
                    Ok(Value::Array(
                        edit::through::through_edits(q)
                            .iter()
                            .map(|e| json!({"track": e.track.0, "left": e.left.0, "right": e.right.0, "time": e.time.0}))
                            .collect(),
                    ))
                }),
                spec("sequence.makeSubsequence", "Make Subsequence", &["Sequence"], Some("Shift+U"), r#"{"name":str?}"#, has_seq, make_subsequence),
            ],
        ),
        (
            "sequence.addTracks",
            vec![spec(
                "sequence.deleteTracks",
                "Delete Tracks…",
                &["Sequence"],
                None,
                r#"{"video":"empty"|"V2"|id?,"audio":"empty"|"A2"|id?}"#,
                has_seq,
                delete_tracks,
            )],
        ),
        (
            "markers.markSelection",
            vec![
                spec("markers.markSplitVideoIn", "Video In", &["Markers", "Mark Split"], None, SPLIT, always, |s, p| mark_split(s, p, Side::VideoIn)),
                spec("markers.markSplitVideoOut", "Video Out", &["Markers", "Mark Split"], None, SPLIT, always, |s, p| mark_split(s, p, Side::VideoOut)),
                spec("markers.markSplitAudioIn", "Audio In", &["Markers", "Mark Split"], None, SPLIT, always, |s, p| mark_split(s, p, Side::AudioIn)),
                spec("markers.markSplitAudioOut", "Audio Out", &["Markers", "Mark Split"], None, SPLIT, always, |s, p| mark_split(s, p, Side::AudioOut)),
            ],
        ),
        (
            "markers.goToOut",
            vec![
                spec("markers.goToSplitVideoIn", "Video In", &["Markers", "Go to Split"], None, r#"{"target":"program|source"}"#, has_split, |s, p| {
                    go_to_split(s, p, Side::VideoIn)
                }),
                spec("markers.goToSplitVideoOut", "Video Out", &["Markers", "Go to Split"], None, r#"{"target":"program|source"}"#, has_split, |s, p| {
                    go_to_split(s, p, Side::VideoOut)
                }),
                spec("markers.goToSplitAudioIn", "Audio In", &["Markers", "Go to Split"], None, r#"{"target":"program|source"}"#, has_split, |s, p| {
                    go_to_split(s, p, Side::AudioIn)
                }),
                spec("markers.goToSplitAudioOut", "Audio Out", &["Markers", "Go to Split"], None, r#"{"target":"program|source"}"#, has_split, |s, p| {
                    go_to_split(s, p, Side::AudioOut)
                }),
            ],
        ),
        (
            "markers.add",
            vec![
                spec(
                    "markers.addRange",
                    "Add Range Marker",
                    &["Markers"],
                    Some("Ctrl+Shift+M"),
                    r#"{"time":ticks?,"durationFrames":i64=1s,"duration":ticks?,"name":str?,"comment":str?,"color":label?}"#,
                    has_seq,
                    add_range_marker,
                ),
                spec(
                    "markers.addRangeInOut",
                    "Add Range Marker to In and Out",
                    &["Markers"],
                    Some("Ctrl+M"),
                    r#"{"name":str?,"comment":str?,"color":label?}"#,
                    has_in_and_out,
                    add_range_marker_in_out,
                ),
            ],
        ),
        (
            "markers.clearAll",
            vec![
                spec("markers.showAllMarkerColors", "Show All Marker Colors", &["Markers"], None, "{}", has_hidden_colors, |s, _| {
                    s.state.hidden_marker_colors.clear();
                    Ok(json!({"hidden": []}))
                }),
                spec("markers.filterColors", "Marker Colour Filter", &[], None, r#"{"hidden":[label]}|{"color":label,"visible":bool?}"#, always, filter_colors),
            ],
        ),
        (
            "markers.edit",
            vec![
                spec(
                    "markers.addChapter",
                    "Add Chapter Marker…",
                    &["Markers"],
                    None,
                    r#"{"time":ticks?,"name":str?,"comment":str?,"color":label?}"#,
                    has_seq,
                    |s, p| {
                        let t = time_p(s, p, "").unwrap_or(s.playhead());
                        let n = s.active_sequence().map(|q| q.markers.iter().filter(|m| m.kind == MarkerKind::Chapter).count()).unwrap_or(0) + 1;
                        let name = str_p(p, "name").map(str::to_string).unwrap_or_else(|| format!("Chapter {n}"));
                        let id = add_marker(s, "Add Chapter Marker", t, Tick::ZERO, MarkerKind::Chapter, name, p)?;
                        Ok(json!({"marker": id.0}))
                    },
                ),
                spec("markers.rippleSequenceMarkers", "Ripple Sequence Markers", &["Markers"], None, r#"{"on":bool?}"#, always, |s, p| {
                    s.state.ripple_sequence_markers = bool_p(p, "on").unwrap_or(!s.state.ripple_sequence_markers);
                    Ok(json!({"rippleSequenceMarkers": s.state.ripple_sequence_markers}))
                }),
                spec(
                    "markers.copyPasteIncludesSequenceMarkers",
                    "Copy Paste Includes Sequence Markers",
                    &["Markers"],
                    None,
                    r#"{"on":bool?}"#,
                    always,
                    |s, p| {
                        s.state.copy_paste_sequence_markers = bool_p(p, "on").unwrap_or(!s.state.copy_paste_sequence_markers);
                        Ok(json!({"copyPasteIncludesSequenceMarkers": s.state.copy_paste_sequence_markers}))
                    },
                ),
            ],
        ),
    ]
}

// ---------- enablement ----------

fn has_source(s: &Session) -> std::result::Result<(), String> {
    has_seq(s)?;
    s.state.source_item.map(|_| ()).ok_or_else(|| "no clip in the Source monitor".into())
}
fn has_in_and_out(s: &Session) -> std::result::Result<(), String> {
    let q = s.active_sequence().ok_or("no sequence is open")?;
    if q.mark_in.is_some() && q.mark_out.is_some() { Ok(()) } else { Err("mark an In and an Out point first".into()) }
}
fn has_split(s: &Session) -> std::result::Result<(), String> {
    let program = s.active_sequence().is_some_and(|q| !q.split.is_empty());
    let source = s.state.source_item.and_then(|i| s.project.item(i)).is_some_and(|i| !i.split.is_empty());
    if program || source { Ok(()) } else { Err("there are no split points".into()) }
}
fn has_hidden_colors(s: &Session) -> std::result::Result<(), String> {
    if s.state.hidden_marker_colors.is_empty() { Err("all marker colours are shown".into()) } else { Ok(()) }
}

// ---------- selection follows playhead ----------

/// Select the clips under the playhead on targeted tracks (with linked partners).
pub fn select_under_playhead(s: &mut Session) {
    let t = s.playhead();
    let tg = s.targeting().targeted;
    let Some(q) = s.active_sequence() else { return };
    let ids: Vec<ClipId> = q.all_tracks().filter(|tr| tg.contains(&tr.id)).filter_map(|tr| tr.item_at(t).map(|i| i.id)).collect();
    s.state.selection = with_links(s, &ids);
}

// ---------- match frame ----------

/// Timeline time at which `it` shows media time `src` (None when it doesn't).
pub fn timeline_time_of(it: &TrackItem, src: Tick) -> Option<Tick> {
    if let Some(h) = it.frame_hold {
        return (h == src).then_some(it.start);
    }
    let speed = it.speed.abs();
    if speed <= 0.0 {
        return None;
    }
    let total = it.source_out() - it.source_in;
    let rel_src = if it.reverse { it.source_in + total - Tick(1) - src } else { src - it.source_in };
    if rel_src < Tick::ZERO || rel_src >= total {
        return None;
    }
    let t = it.start + Tick((rel_src.0 as f64 / speed).floor() as i64);
    (t < it.end()).then_some(t)
}

/// Sequence ▸ Reverse Match Frame: find the Source monitor frame in the active sequence (targeted
/// tracks first, top video track down, then audio) and move the playhead there.
fn reverse_match_frame(s: &mut Session) -> Result<Value> {
    let item = s.state.source_item.ok_or_else(|| EngineError::Other("no clip in the Source monitor".into()))?;
    let src = s.state.source_playhead;
    let tg = s.targeting().targeted;
    let q = s.active_sequence().ok_or(EngineError::NoSequence)?;
    let mut hits: Vec<(bool, Tick, ClipId)> = Vec::new();
    for tr in q.video_tracks.iter().rev().chain(&q.audio_tracks) {
        for it in tr.items.iter().filter(|i| i.item == item) {
            if let Some(t) = timeline_time_of(it, src) {
                hits.push((!tg.contains(&tr.id), t, it.id));
            }
        }
    }
    // stable: targeted first, then track order, then time
    hits.sort_by_key(|h| h.0);
    let (_, t, clip) = *hits.first().ok_or_else(|| EngineError::Other("the Source monitor frame is not used in this sequence".into()))?;
    s.set_playhead(t);
    Ok(json!({"clip": clip.0, "time": s.playhead().0}))
}

// ---------- gaps ----------

fn go_to_gap(s: &mut Session, p: &Value, next: bool, in_track: bool) -> Result<Value> {
    let t = s.playhead();
    let q = s.active_sequence().ok_or(EngineError::NoSequence)?;
    let gaps: Vec<TimeRange> = if in_track {
        let tracks: Vec<TrackId> = match track_p(s, p, "track") {
            Some(id) => vec![id],
            None => s.targeting().targeted,
        };
        q.all_tracks().filter(|tr| tracks.contains(&tr.id)).flat_map(edit::through::track_gaps).collect()
    } else {
        edit::through::sequence_gaps(q)
    };
    let starts = gaps.iter().map(|g| g.start);
    let hit = if next { starts.filter(|g| *g > t).min() } else { starts.filter(|g| *g < t).max() };
    match hit {
        Some(g) => {
            s.set_playhead(g);
            Ok(json!({"time": s.playhead().0}))
        }
        None => Ok(json!({"time": null})),
    }
}

// ---------- through edits ----------

fn join_through_edits(s: &mut Session, p: &Value) -> Result<Value> {
    let all = bool_p(p, "all").unwrap_or(false) || (p.get("clips").is_none() && p.get("clip").is_none() && s.state.selection.is_empty());
    let only = if all { Vec::new() } else { with_links(s, &clips_p(s, p)) };
    let q = s.active_sequence().ok_or(EngineError::NoSequence)?;
    let any = edit::through::through_edits(q)
        .iter()
        .any(|e| (only.is_empty() || only.contains(&e.left) || only.contains(&e.right)) && q.track(e.track).is_some_and(|t| !t.locked));
    if !any {
        return Err(EngineError::Other("there are no through edits to join".into()));
    }
    let n = s.edit_sequence("Join Through Edits", |q, _, st| {
        let n = edit::through::join_through_edits(q, &only);
        st.selection.retain(|c| q.find_item(*c).is_some());
        Ok(n)
    })?;
    Ok(json!({"joined": n}))
}

// ---------- subsequence ----------

/// Sequence ▸ Make Subsequence: a new sequence (same settings and track layout) holding copies of the
/// selected clips, or of everything on targeted tracks between In and Out, trimmed to In/Out when
/// they are set. The new sequence is added to the project and selected; the original is unchanged.
fn make_subsequence(s: &mut Session, p: &Value) -> Result<Value> {
    let seq_id = s.state.active_sequence.ok_or(EngineError::NoSequence)?;
    let q = s.active_sequence().ok_or(EngineError::NoSequence)?.clone();
    let seq_name = s.project.item(seq_id).map(|i| i.name.clone()).unwrap_or_default();
    let sel = with_links(s, &s.state.selection);
    let fd = q.settings.frame_rate.frame_duration();
    let range = (q.mark_in.is_some() || q.mark_out.is_some())
        .then(|| TimeRange::from_bounds(q.mark_in.unwrap_or(Tick::ZERO), q.mark_out.map(|o| o + fd).unwrap_or(q.duration())))
        .filter(|r| r.duration > Tick::ZERO);
    if sel.is_empty() && range.is_none() {
        return Err(EngineError::Other("select clips or mark In and Out first".into()));
    }
    let tg = s.targeting().targeted;
    let mut items: Vec<(TrackKind, usize, TrackItem)> = Vec::new();
    for (kind, tracks) in [(TrackKind::Video, &q.video_tracks), (TrackKind::Audio, &q.audio_tracks)] {
        for (ti, tr) in tracks.iter().enumerate() {
            for it in &tr.items {
                let wanted = if sel.is_empty() { tg.contains(&tr.id) } else { sel.contains(&it.id) };
                if !wanted {
                    continue;
                }
                if let Some(c) = edit::through::clip_item_to(it, range.unwrap_or(it.range())) {
                    items.push((kind, ti, c));
                }
            }
        }
    }
    if items.is_empty() {
        return Err(EngineError::Other("there are no clips between In and Out on the targeted tracks".into()));
    }
    let offset = range.map(|r| r.start).unwrap_or_else(|| items.iter().map(|i| i.2.start).min().unwrap_or_default());
    let name = match str_p(p, "name") {
        Some(n) => n.to_string(),
        None => (1..).map(|k| format!("{seq_name}_Sub_{k:02}")).find(|n| !s.project.items.values().any(|i| &i.name == n)).expect("free name"),
    };
    let id = s.edit("Make Subsequence", |pr, st| {
        let nid = pr.new_sequence(&name, q.settings.clone(), q.video_tracks.len(), q.audio_tracks.len(), None);
        let mut ids = std::collections::HashMap::new();
        let mut links = std::collections::HashMap::new();
        let mut placed: Vec<(TrackKind, usize, TrackItem)> = Vec::new();
        for (kind, ti, mut it) in items.clone() {
            let new_id = ClipId(pr.alloc_id());
            ids.insert(it.id, new_id);
            it.id = new_id;
            it.start -= offset;
            if let Some(l) = it.link {
                it.link = Some(*links.entry(l).or_insert_with(|| pr.alloc_id()));
            }
            placed.push((kind, ti, it));
        }
        let mut transitions = Vec::new();
        for (kind, tracks) in [(TrackKind::Video, &q.video_tracks), (TrackKind::Audio, &q.audio_tracks)] {
            for (ti, tr) in tracks.iter().enumerate() {
                for trn in &tr.transitions {
                    let from = trn.from.map(|c| ids.get(&c).copied());
                    let to = trn.to.map(|c| ids.get(&c).copied());
                    let inside = range.is_none_or(|r| trn.start >= r.start && trn.end() <= r.end());
                    if inside && from != Some(None) && to != Some(None) {
                        let mut t = trn.clone();
                        t.id = lumiere_codecbase_project::TransitionId(pr.alloc_id());
                        t.from = from.flatten();
                        t.to = to.flatten();
                        t.start -= offset;
                        transitions.push((kind, ti, t));
                    }
                }
            }
        }
        let nq = pr.sequence_mut(nid).ok_or(EngineError::NoSequence)?;
        for (i, tr) in nq.video_tracks.iter_mut().enumerate() {
            tr.name = q.video_tracks[i].name.clone();
        }
        for (i, tr) in nq.audio_tracks.iter_mut().enumerate() {
            tr.name = q.audio_tracks[i].name.clone();
            tr.channels = q.audio_tracks[i].channels;
        }
        for (kind, ti, it) in placed {
            nq.tracks_mut(kind)[ti].items.push(it);
        }
        for (kind, ti, t) in transitions {
            nq.tracks_mut(kind)[ti].transitions.push(t);
        }
        for tr in nq.all_tracks_mut() {
            tr.sort();
            edit::remove_orphan_transitions(tr);
        }
        nq.check().map_err(EngineError::Other)?;
        st.project_selection = vec![nid];
        Ok(nid)
    })?;
    Ok(json!({"sequence": id.0, "name": name}))
}

// ---------- delete tracks ----------

/// Sequence ▸ Delete Tracks…: delete all empty video / audio tracks (`"empty"`) or one track per
/// kind. A sequence always keeps at least one video and one audio track.
fn delete_tracks(s: &mut Session, p: &Value) -> Result<Value> {
    let seq_id = s.state.active_sequence.ok_or(EngineError::NoSequence)?;
    let q = s.active_sequence().ok_or(EngineError::NoSequence)?;
    let mut doomed: Vec<TrackId> = Vec::new();
    for (key, kind) in [("video", TrackKind::Video), ("audio", TrackKind::Audio)] {
        let tracks = q.tracks(kind);
        let mut mine: Vec<TrackId> = match p.get(key) {
            None | Some(Value::Null) => continue,
            Some(v) if v.as_str().is_some_and(|x| matches!(x, "empty" | "allEmpty" | "all-empty")) => {
                tracks.iter().filter(|t| t.items.is_empty()).map(|t| t.id).collect()
            }
            Some(_) => {
                let id = track_p(s, p, key).ok_or_else(|| bad("sequence.deleteTracks", format!("unknown {key} track")))?;
                if !tracks.iter().any(|t| t.id == id) {
                    return Err(bad("sequence.deleteTracks", format!("`{key}` must name a {key} track")));
                }
                vec![id]
            }
        };
        if mine.len() == tracks.len() {
            // keep the first one
            mine.retain(|id| *id != tracks[0].id);
        }
        doomed.extend(mine);
    }
    if doomed.is_empty() {
        return Err(EngineError::Other("no tracks to delete".into()));
    }
    let n = doomed.len();
    s.edit_sequence("Delete Tracks", |q, _, st| {
        q.video_tracks.retain(|t| !doomed.contains(&t.id));
        q.audio_tracks.retain(|t| !doomed.contains(&t.id));
        st.selection.retain(|c| q.find_item(*c).is_some());
        if let Some(tg) = st.targeting.get_mut(&seq_id) {
            tg.targeted.retain(|t| !doomed.contains(t));
            if tg.video_dest.is_some_and(|t| doomed.contains(&t)) {
                tg.video_dest = q.video_tracks.first().map(|t| t.id);
            }
            if tg.audio_dest.is_some_and(|t| doomed.contains(&t)) {
                tg.audio_dest = q.audio_tracks.first().map(|t| t.id);
            }
        }
        Ok(())
    })?;
    Ok(json!({"deleted": n}))
}

// ---------- split points ----------

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Side {
    VideoIn,
    VideoOut,
    AudioIn,
    AudioOut,
}

impl Side {
    fn label(self) -> &'static str {
        match self {
            Side::VideoIn => "Video In",
            Side::VideoOut => "Video Out",
            Side::AudioIn => "Audio In",
            Side::AudioOut => "Audio Out",
        }
    }
    /// Set this split point; a split point of the same channel on the wrong side of it is cleared.
    fn set(self, m: &mut SplitMarks, t: Tick) {
        match self {
            Side::VideoIn => {
                m.video_in = Some(t);
                m.video_out = m.video_out.filter(|o| *o >= t);
            }
            Side::VideoOut => {
                m.video_out = Some(t);
                m.video_in = m.video_in.filter(|i| *i <= t);
            }
            Side::AudioIn => {
                m.audio_in = Some(t);
                m.audio_out = m.audio_out.filter(|o| *o >= t);
            }
            Side::AudioOut => {
                m.audio_out = Some(t);
                m.audio_in = m.audio_in.filter(|i| *i <= t);
            }
        }
    }
    /// The effective point (the split point, or the ordinary In/Out).
    fn get(self, m: &SplitMarks, mark_in: Option<Tick>, mark_out: Option<Tick>) -> Option<Tick> {
        match self {
            Side::VideoIn => m.video_in_or(mark_in),
            Side::VideoOut => m.video_out_or(mark_out),
            Side::AudioIn => m.audio_in_or(mark_in),
            Side::AudioOut => m.audio_out_or(mark_out),
        }
    }
}

fn source_marks(s: &Session, item: ItemId) -> (Option<Tick>, Option<Tick>) {
    match s.project.item(item).map(|i| &i.kind) {
        Some(lumiere_codecbase_project::ItemKind::Media(m)) => (m.mark_in, m.mark_out),
        Some(lumiere_codecbase_project::ItemKind::Sequence(q)) => (q.mark_in, q.mark_out),
        _ => (None, None),
    }
}

fn mark_split(s: &mut Session, p: &Value, side: Side) -> Result<Value> {
    let label = format!("Mark Split {}", side.label());
    if str_p(p, "target") == Some("source") {
        let item = s.state.source_item.ok_or_else(|| EngineError::Other("no clip in the Source monitor".into()))?;
        let t = p.get("time").and_then(Value::as_i64).map(Tick).unwrap_or(s.state.source_playhead);
        let split = s.edit(&label, |pr, _| {
            let it = pr.item_mut(item).ok_or_else(|| EngineError::Other("no such item".into()))?;
            side.set(&mut it.split, t);
            Ok(it.split)
        })?;
        return Ok(serde_json::to_value(split).unwrap_or_default());
    }
    s.active_sequence().ok_or(EngineError::NoSequence)?;
    let t = time_p(s, p, "").unwrap_or(s.playhead());
    let split = s.edit_sequence(&label, |q, _, _| {
        side.set(&mut q.split, t);
        Ok(q.split)
    })?;
    Ok(serde_json::to_value(split).unwrap_or_default())
}

fn go_to_split(s: &mut Session, p: &Value, side: Side) -> Result<Value> {
    let none = || EngineError::Other(format!("there is no {} point", side.label()));
    if str_p(p, "target") == Some("source") {
        let item = s.state.source_item.ok_or_else(|| EngineError::Other("no clip in the Source monitor".into()))?;
        let (mi, mo) = source_marks(s, item);
        let split = s.project.item(item).map(|i| i.split).unwrap_or_default();
        let t = side.get(&split, mi, mo).ok_or_else(none)?;
        s.state.source_playhead = t;
        return Ok(json!({"time": t.0}));
    }
    let q = s.active_sequence().ok_or(EngineError::NoSequence)?;
    let t = side.get(&q.split, q.mark_in, q.mark_out).ok_or_else(none)?;
    s.set_playhead(t);
    Ok(json!({"time": s.playhead().0}))
}

/// Source ranges for Insert / Overwrite with split points: (video range, audio range). Equal when
/// the source has no split points. `full` is the range the ordinary In/Out give.
pub(crate) fn split_source_ranges(s: &Session, item: ItemId, full: TimeRange) -> (TimeRange, TimeRange) {
    let Some(pi) = s.project.item(item) else { return (full, full) };
    let sp = pi.split;
    if sp.is_empty() {
        return (full, full);
    }
    let fd = pi.frame_rate().frame_duration();
    let range = |i: Option<Tick>, o: Option<Tick>| {
        let a = i.unwrap_or(full.start);
        let b = o.map(|o| o + fd).unwrap_or(full.end());
        TimeRange::from_bounds(a, b.max(a + fd))
    };
    (range(sp.video_in, sp.video_out), range(sp.audio_in, sp.audio_out))
}

// ---------- markers ----------

/// Move sequence markers for a ripple edit at `from` by `shift`: later markers move; with a
/// negative shift, markers in the removed span `[from + shift, from)` are deleted.
pub fn ripple_markers(markers: &mut Vec<Marker>, from: Tick, shift: Tick) {
    if shift == Tick::ZERO {
        return;
    }
    if shift < Tick::ZERO {
        markers.retain(|m| !(m.start >= from + shift && m.start < from));
    }
    for m in markers.iter_mut().filter(|m| m.start >= from) {
        m.start += shift;
    }
    markers.sort_by_key(|m| m.start);
}

fn add_marker(s: &mut Session, label: &str, t: Tick, dur: Tick, kind: MarkerKind, name: String, p: &Value) -> Result<MarkerId> {
    let comment = str_p(p, "comment").unwrap_or("").to_string();
    let color = str_p(p, "color").and_then(Label::from_name).unwrap_or(Label::Green);
    s.edit_sequence(label, |q, ctx, _| {
        let id = MarkerId(ctx.alloc());
        q.markers.push(Marker { id, start: t, duration: dur, name, comment, kind, color });
        q.markers.sort_by_key(|m| m.start);
        Ok(id)
    })
}

/// Markers ▸ Add Range Marker: a marker with a duration (one second unless `durationFrames` or
/// `duration` say otherwise) at the playhead.
fn add_range_marker(s: &mut Session, p: &Value) -> Result<Value> {
    let rate = s.sequence_rate();
    let t = time_p(s, p, "").unwrap_or(s.playhead());
    let dur = p
        .get("duration")
        .and_then(Value::as_i64)
        .map(Tick)
        .or_else(|| p.get("durationFrames").and_then(Value::as_i64).map(|f| rate.tick_of(f)))
        .unwrap_or(rate.tick_of(rate.timecode_base()));
    if dur <= Tick::ZERO {
        return Err(bad("markers.addRange", "the duration must be positive"));
    }
    let name = str_p(p, "name").unwrap_or("").to_string();
    let id = add_marker(s, "Add Range Marker", t, dur, MarkerKind::Comment, name, p)?;
    Ok(json!({"marker": id.0}))
}

/// Markers ▸ Add Range Marker to In and Out: a marker spanning the sequence In to Out (inclusive).
fn add_range_marker_in_out(s: &mut Session, p: &Value) -> Result<Value> {
    let q = s.active_sequence().ok_or(EngineError::NoSequence)?;
    let (Some(i), Some(o)) = (q.mark_in, q.mark_out) else { return Err(EngineError::Other("mark an In and an Out point first".into())) };
    let dur = o + q.settings.frame_rate.frame_duration() - i;
    let name = str_p(p, "name").unwrap_or("").to_string();
    let id = add_marker(s, "Add Range Marker", i, dur, MarkerKind::Comment, name, p)?;
    Ok(json!({"marker": id.0}))
}

fn filter_colors(s: &mut Session, p: &Value) -> Result<Value> {
    if let Some(a) = p.get("hidden").and_then(Value::as_array) {
        let mut v = Vec::new();
        for x in a {
            let n = x.as_str().unwrap_or_default();
            v.push(Label::from_name(n).ok_or_else(|| bad("markers.filterColors", format!("unknown colour `{n}`")))?);
        }
        s.state.hidden_marker_colors = v;
    } else if let Some(n) = str_p(p, "color") {
        let c = Label::from_name(n).ok_or_else(|| bad("markers.filterColors", format!("unknown colour `{n}`")))?;
        let hidden = s.state.hidden_marker_colors.contains(&c);
        let visible = bool_p(p, "visible").unwrap_or(hidden);
        s.state.hidden_marker_colors.retain(|x| *x != c);
        if !visible {
            s.state.hidden_marker_colors.push(c);
        }
    } else {
        return Err(bad("markers.filterColors", "need `hidden` or `color`"));
    }
    Ok(json!({"hidden": s.state.hidden_marker_colors.iter().map(|c| c.name()).collect::<Vec<_>>()}))
}
