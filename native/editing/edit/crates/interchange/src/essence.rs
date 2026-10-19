//! Media options shared by the AAF and OMF exporters, and the audio essence the caller supplies.
//!
//! This crate does no file I/O and decodes no media. An export that embeds audio, consolidates
//! (trims) it into new files, renders clip effects or mixes the video down works in three steps:
//!
//! 1. [`audio_needs`] lists the audio ranges the document will reference (with handles);
//! 2. the caller (the engine) decodes / renders each range and writes or keeps the PCM;
//! 3. the caller passes the results as [`AudioEssence`] in [`MediaOptions::essence`] (and a
//!    rendered video file as [`MediaOptions::mixdown_video`]) to [`crate::aaf::export`] /
//!    [`crate::omf::export`].

use std::collections::BTreeMap;

use lumiere_edit_project::{ClipId, ItemId, ItemKind, MediaRef, Project, TrackKind};
use lumiere_edit_time::Tick;
use serde::{Deserialize, Serialize};

/// What a piece of essence stands in for: a whole media item, or one clip's rendered audio.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub enum EssenceKey {
    Media(ItemId),
    Clip(ClipId),
}

/// One range of source audio an export references.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct AudioNeed {
    pub key: EssenceKey,
    /// The media item (for a clip key: the clip's media).
    pub item: ItemId,
    /// The media file, when the item is file media.
    pub path: Option<String>,
    /// Media time range to supply (handles included, clamped to the media).
    pub start: Tick,
    pub end: Tick,
    /// Channels of the media's audio.
    pub channels: u32,
    pub sample_rate: u32,
}

/// Where the samples of an [`AudioEssence`] are.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub enum EssenceData {
    /// Interleaved little-endian signed PCM (`bits` per sample), embedded in the document.
    Embedded(Vec<u8>),
    /// A media file the caller wrote (consolidated / trimmed / rendered), referenced by path.
    File { path: String },
}

/// Audio essence supplied by the caller for one [`EssenceKey`] (and channel, when broken out to
/// mono).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct AudioEssence {
    pub key: EssenceKey,
    /// `Some(c)`: this essence carries only source channel `c` (0-based), as one mono stream.
    pub channel: Option<u32>,
    /// Media time of the first sample (trimmed essence starts after the media's 0).
    pub start: Tick,
    /// Sample frames.
    pub frames: u64,
    pub sample_rate: u32,
    pub bits: u16,
    pub channels: u32,
    pub data: EssenceData,
    /// Clip effects (volume, gain, audio effects) are rendered into these samples: the document
    /// carries no gain for clips that use it.
    pub effects_rendered: bool,
}

/// A rendered video mixdown that replaces every video track with one clip.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct MixdownVideo {
    pub path: String,
    /// Timeline range the file covers (its first frame is at `start`).
    pub start: Tick,
    pub duration: Tick,
    pub width: u32,
    pub height: u32,
}

/// Options shared by the AAF and OMF exporters.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct MediaOptions {
    /// Split multichannel audio into mono tracks (one slot per channel; essence per channel).
    pub breakout_to_mono: bool,
    /// Leave the video tracks out (OMF is audio-only).
    pub audio_only: bool,
    /// Essence the caller prepared (see the module docs). Media without essence is referenced at
    /// its original path.
    pub essence: Vec<AudioEssence>,
    /// Replace the video tracks with this rendered file.
    pub mixdown_video: Option<MixdownVideo>,
}

impl MediaOptions {
    /// Essence for `key` (and `channel` when broken out).
    pub fn essence_for(&self, key: EssenceKey, channel: Option<u32>) -> Option<&AudioEssence> {
        self.essence.iter().find(|e| e.key == key && (e.channel == channel || e.channel.is_none()))
    }
}

/// How [`audio_needs`] groups ranges.
#[derive(Clone, Copy, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct NeedOptions {
    /// Extra media before and after every used range.
    pub handles: Tick,
    /// One range per clip ([`EssenceKey::Clip`]; for rendering clip effects) instead of one range
    /// per media item covering all its uses ([`EssenceKey::Media`]).
    pub per_clip: bool,
    /// Whole media instead of the used range (embedding / copying without trimming).
    pub whole_media: bool,
}

/// The audio ranges the audio tracks of `sequence` reference, in a stable order.
pub fn audio_needs(project: &Project, sequence: ItemId, opts: &NeedOptions) -> Vec<AudioNeed> {
    let Some(seq) = project.sequence(sequence) else { return Vec::new() };
    let mut by_item: BTreeMap<EssenceKey, AudioNeed> = BTreeMap::new();
    for t in seq.tracks(TrackKind::Audio) {
        for c in &t.items {
            let item = crate::common::base_item(project, c.item);
            let Some(m) = project.item(item).and_then(|i| match &i.kind {
                ItemKind::Media(m) => Some(m),
                _ => None,
            }) else {
                continue;
            };
            let Some(a) = m.info.audio.as_ref() else { continue };
            let path = match &m.media {
                MediaRef::File { path } => Some(path.clone()),
                MediaRef::Generator(_) => None,
            };
            let dur = m.info.duration;
            let (lo, hi) = if c.speed < 0.0 || c.reverse { (c.source_out(), c.source_in) } else { (c.source_in, c.source_out()) };
            let (lo, hi) = (lo.min(hi), lo.max(hi));
            let (mut start, mut end) = ((lo - opts.handles).max(Tick::ZERO), hi + opts.handles);
            if dur > Tick::ZERO {
                end = end.min(dur);
            }
            if opts.whole_media {
                start = Tick::ZERO;
                end = dur.max(end);
            }
            let key = if opts.per_clip { EssenceKey::Clip(c.id) } else { EssenceKey::Media(item) };
            let e = by_item.entry(key).or_insert_with(|| AudioNeed { key, item, path, start, end, channels: a.channels, sample_rate: a.sample_rate });
            e.start = e.start.min(start);
            e.end = e.end.max(end);
        }
    }
    by_item.into_values().collect()
}
