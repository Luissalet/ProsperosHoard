//! `.lumedit` project files.
//!
//! A project file is UTF-8 JSON with an explicit schema version:
//!
//! ```json
//! { "format": "lumiere-codecbase.project", "schema_version": 2, "generator": "Lumiere Edit 0.1.0", "project": { … } }
//! ```
//!
//! - **Versioning.** [`SCHEMA_VERSION`] is the version this build writes. Older files are upgraded on
//!   load by a chain of single-step migrations ([`MIGRATIONS`]: v1→v2, v2→v3, …) operating on the
//!   JSON document, so every historical shape only needs one small function. Newer files are refused
//!   with [`FormatError::TooNew`] instead of being half-read (and later overwritten with data loss).
//! - **Atomic saves.** [`atomic_write`] writes a temp file in the destination directory, fsyncs it,
//!   renames it over the target and fsyncs the directory: a crash or power cut during a save leaves
//!   either the old file or the new one, never a torn mix.
//! - **Auto-save naming/rotation** lives in [`autosave`] (`<name>-YYYY-MM-DD_HH-MM-SS.lumedit` in an
//!   `Auto-Save` folder next to the project, oldest pruned beyond the version limit).
//!
//! Schema history:
//! - **v1** (M0–M11): the bare serialized `Project` object with a `"version": 1` field.
//! - **v2** (M11.1): the envelope above; `Project.version` dropped (the envelope carries it).
//! - **v3** (M10.2): graphic clips (`ItemKind::Graphic`, text/shape layers). The shape of older
//!   data is unchanged (no-op step); the bump makes builds without graphics refuse such files
//!   cleanly instead of failing to parse them.
//! - **v4** (M7.4): Essential Sound (clip audio types and settings, `EffectInstance::essential`).
//!   No-op step; older builds would otherwise drop these fields silently when saving.
//! - **v5** (M8.8): colour management (sequence working space, Interpret Footage colour space,
//!   project LUT library). No-op step, for the same reason.
//! - **v6** (M11.7/M11.9): media identity fingerprints and ingest settings. No-op step.
//! - **v7** (M5.5): effect masks (keyframable Bézier mask paths on effect instances). No-op step.
//! - **v8** (M12): multi-camera source sequences, multi-camera clips and merged clips. No-op step;
//!   older builds would drop the camera data when saving.
//! - **v12** (M10.7): graphics templates, rolls / crawls, responsive design (pins, intro / outro),
//!   per-character text styles (`TrackItem::graphic`, `EffectInstance::layer`) and source graphics
//!   (`Project::source_graphics`). No-op step; older builds would drop these fields when saving.

pub mod atomic;
pub mod autosave;

use lumiere_codecbase_project::Project;
use serde::{Deserialize, Serialize};
use serde_json::Value;

pub use atomic::atomic_write;

/// The `format` tag written in every envelope.
pub const FORMAT_ID: &str = "lumiere-codecbase.project";

/// A migration upgrades a whole document from schema `n` to `n + 1`.
pub type Migration = fn(Value) -> Result<Value, String>;

/// `MIGRATIONS[i]` upgrades schema `i + 1` to `i + 2`. Append one function per schema bump; never
/// edit a shipped one.
pub const MIGRATIONS: &[Migration] = &[v1_to_v2, v2_to_v3, v3_to_v4, v4_to_v5, v5_to_v6, v6_to_v7, v7_to_v8, v8_to_v9, v9_to_v10, v10_to_v11, v11_to_v12];

/// The schema version this build writes (and the newest it reads).
pub const SCHEMA_VERSION: u32 = MIGRATIONS.len() as u32 + 1;

/// Oldest schema this build can still upgrade.
pub const OLDEST_SCHEMA: u32 = 1;

#[derive(Debug, thiserror::Error)]
pub enum FormatError {
    #[error("not a Lumiere Edit project: {0}")]
    NotAProject(String),
    #[error(
        "this project was saved by a newer version of Lumiere Edit (project schema v{found}); this build reads up to v{supported}. Update Lumiere Edit to open it."
    )]
    TooNew { found: u32, supported: u32 },
    #[error("project schema v{found} is too old for this build (oldest supported: v{oldest})")]
    TooOld { found: u32, oldest: u32 },
    #[error("upgrading project from schema v{from} to v{}: {msg}", from + 1)]
    Migration { from: u32, msg: String },
    #[error("project file is damaged: {0}")]
    Corrupt(String),
}

/// A project read from disk.
#[derive(Debug)]
pub struct Loaded {
    pub project: Project,
    /// Schema version found in the file (before migration).
    pub schema_version: u32,
    /// The `generator` string of the writer, if recorded.
    pub generator: Option<String>,
}

impl Loaded {
    /// True when the file was written in an older schema and upgraded on load.
    pub fn migrated(&self) -> bool {
        self.schema_version < SCHEMA_VERSION
    }
}

/// Writer identification stored in the envelope.
pub fn generator() -> String {
    format!("Lumiere Edit {}", env!("CARGO_PKG_VERSION"))
}

#[derive(Serialize)]
struct EnvelopeOut<'a> {
    format: &'static str,
    schema_version: u32,
    generator: String,
    project: &'a Project,
}

#[derive(Deserialize)]
struct EnvelopeIn {
    #[serde(default)]
    generator: Option<String>,
    project: Project,
}

/// Just enough of a document to know which schema it is.
#[derive(Deserialize)]
struct Probe {
    #[serde(default)]
    format: Option<String>,
    #[serde(default)]
    schema_version: Option<u32>,
    /// Bare (pre-envelope) project: its `version` field.
    #[serde(default)]
    version: Option<u32>,
    #[serde(default)]
    items: Option<serde::de::IgnoredAny>,
    #[serde(default)]
    root: Option<serde::de::IgnoredAny>,
}

/// Serialize a project in the current schema. `pretty` = indented (human-diffable; used for the
/// project file itself), compact otherwise (recovery snapshots).
pub fn encode(project: &Project, pretty: bool) -> Vec<u8> {
    let env = EnvelopeOut { format: FORMAT_ID, schema_version: SCHEMA_VERSION, generator: generator(), project };
    let r = if pretty { serde_json::to_vec_pretty(&env) } else { serde_json::to_vec(&env) };
    // Serializing plain data (string keys, finite numbers or null) cannot fail.
    r.unwrap_or_default()
}

/// Detect the schema version of a parsed document.
pub fn detect_version(doc: &Value) -> Result<u32, FormatError> {
    let probe: Probe = serde_json::from_value(doc.clone()).map_err(|e| FormatError::NotAProject(e.to_string()))?;
    probe_version(&probe)
}

fn probe_version(p: &Probe) -> Result<u32, FormatError> {
    if let Some(v) = p.schema_version {
        if let Some(f) = &p.format
            && f != FORMAT_ID
        {
            return Err(FormatError::NotAProject(format!("format is `{f}`, expected `{FORMAT_ID}`")));
        }
        return Ok(v);
    }
    // Before the envelope (v1) a file was the bare Project object with `version`.
    // Its `version` was 1, or 2 on builds that briefly bumped it when caption tracks were added
    // (same shape; the new field loads with its default). Anything higher is from the future.
    if p.items.is_some() && p.root.is_some() {
        return Ok(match p.version.unwrap_or(1) {
            0..=2 => 1,
            v => v,
        });
    }
    Err(FormatError::NotAProject("no `schema_version` and not a legacy project".into()))
}

/// Read a project file (any supported schema).
pub fn decode(bytes: &[u8]) -> Result<Loaded, FormatError> {
    let probe: Probe = serde_json::from_slice(bytes)
        .map_err(|e| if e.is_syntax() || e.is_eof() { FormatError::Corrupt(e.to_string()) } else { FormatError::NotAProject(e.to_string()) })?;
    let found = probe_version(&probe)?;
    check_supported(found)?;
    if found == SCHEMA_VERSION {
        // Fast path: deserialize straight into the model.
        let env: EnvelopeIn = serde_json::from_slice(bytes).map_err(|e| FormatError::Corrupt(e.to_string()))?;
        return Ok(Loaded { project: env.project, schema_version: found, generator: env.generator });
    }
    let doc: Value = serde_json::from_slice(bytes).map_err(|e| FormatError::Corrupt(e.to_string()))?;
    let doc = migrate_with(MIGRATIONS, doc, found)?;
    let env: EnvelopeIn = serde_json::from_value(doc).map_err(|e| FormatError::Corrupt(format!("after upgrading from schema v{found}: {e}")))?;
    Ok(Loaded { project: env.project, schema_version: found, generator: env.generator })
}

fn check_supported(found: u32) -> Result<(), FormatError> {
    if found > SCHEMA_VERSION {
        return Err(FormatError::TooNew { found, supported: SCHEMA_VERSION });
    }
    if found < OLDEST_SCHEMA {
        return Err(FormatError::TooOld { found, oldest: OLDEST_SCHEMA });
    }
    Ok(())
}

/// Run `table` from schema `from` up to `table.len() + 1`, stamping `schema_version` after each step.
pub fn migrate_with(table: &[Migration], mut doc: Value, from: u32) -> Result<Value, FormatError> {
    let target = table.len() as u32 + 1;
    if from > target {
        return Err(FormatError::TooNew { found: from, supported: target });
    }
    if from == 0 {
        return Err(FormatError::TooOld { found: 0, oldest: 1 });
    }
    for v in from..target {
        let step = table[(v - 1) as usize];
        doc = step(doc).map_err(|msg| FormatError::Migration { from: v, msg })?;
        match doc.as_object_mut() {
            Some(o) => {
                o.insert("schema_version".into(), Value::from(v + 1));
            }
            None => return Err(FormatError::Migration { from: v, msg: "migration produced a non-object document".into() }),
        }
    }
    Ok(doc)
}

/// v1 → v2: wrap the bare project in the envelope and drop the per-project `version` field.
fn v1_to_v2(doc: Value) -> Result<Value, String> {
    let Value::Object(mut project) = doc else { return Err("expected a JSON object".into()) };
    project.remove("version");
    Ok(serde_json::json!({
        "format": FORMAT_ID,
        "schema_version": 2,
        "generator": "Lumiere Edit (schema v1)",
        "project": Value::Object(project),
    }))
}

/// v2 → v3: graphic clips added; existing data needs no change.
fn v2_to_v3(doc: Value) -> Result<Value, String> {
    Ok(doc)
}

/// v3 → v4: Essential Sound fields added; existing data needs no change.
fn v3_to_v4(doc: Value) -> Result<Value, String> {
    Ok(doc)
}

/// v4 → v5: colour-management fields added; existing data needs no change.
fn v4_to_v5(doc: Value) -> Result<Value, String> {
    Ok(doc)
}

/// v5 → v6: media identity and ingest settings added; existing data needs no change.
fn v5_to_v6(doc: Value) -> Result<Value, String> {
    Ok(doc)
}

/// v6 → v7: effect masks (Bézier mask paths as keyframable parameters); existing data needs no change.
fn v6_to_v7(doc: Value) -> Result<Value, String> {
    Ok(doc)
}

/// v7 → v8: multi-camera source sequences / clips and merged clips added; existing data needs no
/// change.
fn v7_to_v8(doc: Value) -> Result<Value, String> {
    Ok(doc)
}

/// v8 → v9: per-media-item transcripts (`project.transcripts`) added; existing data needs no change.
fn v8_to_v9(doc: Value) -> Result<Value, String> {
    Ok(doc)
}

/// v9 → v10: clip time interpolation, Hold Filters, Field Options and audio source channels; the
/// Modify ▸ Audio Channels map; subclips' Restrict Trims flag. Existing data needs no change.
fn v9_to_v10(doc: Value) -> Result<Value, String> {
    Ok(doc)
}

/// v10 → v11: search bins (`project.search_bins`), Flash Cue markers, Project Settings safe areas,
/// capture format and scratch disks. Existing data needs no change.
fn v10_to_v11(doc: Value) -> Result<Value, String> {
    Ok(doc)
}

/// v11 → v12: graphics templates, rolls / crawls, responsive design and per-character text styles
/// on graphic clips, and source graphics. Existing data needs no change.
fn v11_to_v12(doc: Value) -> Result<Value, String> {
    Ok(doc)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn schema_version_matches_table() {
        assert_eq!(SCHEMA_VERSION, 12);
    }

    #[test]
    fn roundtrip_current() {
        let p = Project::new("Round Trip");
        for pretty in [true, false] {
            let b = encode(&p, pretty);
            let l = decode(&b).unwrap();
            assert_eq!(l.project, p);
            assert_eq!(l.schema_version, SCHEMA_VERSION);
            assert!(!l.migrated());
            assert_eq!(l.generator.as_deref(), Some(generator().as_str()));
        }
    }

    #[test]
    fn envelope_has_explicit_schema_version() {
        let v: Value = serde_json::from_slice(&encode(&Project::new("x"), true)).unwrap();
        assert_eq!(v["schema_version"], SCHEMA_VERSION);
        assert_eq!(v["format"], FORMAT_ID);
        assert!(v["project"]["items"].is_object());
        assert!(v["project"].get("version").is_none());
    }

    #[test]
    fn refuses_newer() {
        let mut v: Value = serde_json::from_slice(&encode(&Project::new("x"), false)).unwrap();
        v["schema_version"] = Value::from(SCHEMA_VERSION + 1);
        let e = decode(&serde_json::to_vec(&v).unwrap()).unwrap_err();
        assert!(matches!(e, FormatError::TooNew { .. }));
        assert!(e.to_string().contains("newer version of Lumiere Edit"), "{e}");
    }

    #[test]
    fn rejects_foreign_and_damaged() {
        assert!(matches!(decode(br#"{"hello": 1}"#), Err(FormatError::NotAProject(_))));
        assert!(matches!(decode(br#"{"format":"other","schema_version":1}"#), Err(FormatError::NotAProject(_))));
        let b = encode(&Project::new("x"), false);
        assert!(matches!(decode(&b[..b.len() / 2]), Err(FormatError::Corrupt(_))));
    }

    #[test]
    fn chain_runs_each_step_in_order() {
        fn a(mut d: Value) -> Result<Value, String> {
            d["trail"] = Value::from(format!("{}a", d["trail"].as_str().unwrap_or("")));
            Ok(d)
        }
        fn b(mut d: Value) -> Result<Value, String> {
            d["trail"] = Value::from(format!("{}b", d["trail"].as_str().unwrap_or("")));
            Ok(d)
        }
        fn fail(_: Value) -> Result<Value, String> {
            Err("boom".into())
        }
        let table: &[Migration] = &[a, b, a];
        let out = migrate_with(table, serde_json::json!({}), 1).unwrap();
        assert_eq!(out["trail"], "aba");
        assert_eq!(out["schema_version"], 4);
        let out = migrate_with(table, serde_json::json!({}), 3).unwrap();
        assert_eq!(out["trail"], "a");
        let out = migrate_with(table, serde_json::json!({"x": 1}), 4).unwrap();
        assert_eq!(out, serde_json::json!({"x": 1}));
        assert!(matches!(migrate_with(table, serde_json::json!({}), 5), Err(FormatError::TooNew { found: 5, supported: 4 })));
        let e = migrate_with(&[a, fail], serde_json::json!({}), 1).unwrap_err();
        assert!(matches!(e, FormatError::Migration { from: 2, .. }), "{e}");
    }
}
