//! Golden-image tests for Lumiere Edit's renderer (test-only crate; the library is empty).
//!
//! `tests/golden.rs` builds small procedural projects (no media files), renders one frame of each
//! through the CPU reference compositor (`lumiere-codecbase-render`) and compares it with a committed PNG
//! in `goldens/` using `lumiere_codecbase_testkit::golden`. The same scenes are composited on the GPU
//! (`lumiere-codecbase-gpu`) when an adapter is available and compared with the CPU result.
//!
//! Regenerate references with `FILMCRAFT_BLESS=1 cargo test -p lumiere-codecbase-golden`.
