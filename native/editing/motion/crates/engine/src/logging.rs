//! Help ▸ Enable Logging / Reveal Logging File: a `log` logger that, while enabled, appends
//! every record to `Logs/Lumiere Motion Log.txt` in the settings folder (or the temporary folder),
//! and always keeps the last few hundred warnings and errors in memory for the System
//! Compatibility Report.
//!
//! Frontends call [`install`] once at startup; [`set_enabled`] (the `help.enableLogging`
//! command) turns file logging on and off.

use std::collections::VecDeque;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::sync::atomic::{AtomicBool, Ordering};

static ENABLED: AtomicBool = AtomicBool::new(false);
static STATE: Mutex<State> = Mutex::new(State { file: None, recent: VecDeque::new() });

struct State {
    file: Option<PathBuf>,
    recent: VecDeque<String>,
}

struct Logger;

const RECENT: usize = 300;

impl log::Log for Logger {
    fn enabled(&self, m: &log::Metadata) -> bool {
        m.level() <= log::Level::Warn || (ENABLED.load(Ordering::Relaxed) && m.target().starts_with("lumiere-motion"))
    }
    fn log(&self, r: &log::Record) {
        if !self.enabled(r.metadata()) {
            return;
        }
        let line = format!("{} {:<5} {}: {}", unix_secs(), r.level(), r.target(), r.args());
        let Ok(mut st) = STATE.lock() else { return };
        if r.level() <= log::Level::Warn {
            st.recent.push_back(line.clone());
            if st.recent.len() > RECENT {
                st.recent.pop_front();
            }
        }
        if ENABLED.load(Ordering::Relaxed)
            && let Some(f) = &st.file
            && let Ok(mut out) = std::fs::OpenOptions::new().create(true).append(true).open(f)
        {
            let _ = writeln!(out, "{line}");
        }
    }
    fn flush(&self) {}
}

fn unix_secs() -> u64 {
    web_time::SystemTime::now().duration_since(web_time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

/// Install the logger (once; later calls and other installed loggers are left alone).
pub fn install() {
    static LOGGER: Logger = Logger;
    if log::set_logger(&LOGGER).is_ok() {
        log::set_max_level(log::LevelFilter::Info);
    }
}

/// Log every panic (message and location) through the logger, then run the previous hook
/// (which prints it). Panics in commands are then caught by [`crate::guard::guarded`].
pub fn install_panic_hook() {
    let previous = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        log::error!("panic: {info}");
        previous(info);
    }));
}

/// The log file inside `config_dir` (the settings folder), or the temporary folder.
pub fn log_path(config_dir: Option<&Path>) -> PathBuf {
    config_dir.map(Path::to_path_buf).unwrap_or_else(std::env::temp_dir).join("Logs").join("Lumiere Motion Log.txt")
}

/// Turn file logging on (writing to `file`) or off.
pub fn set_enabled(on: bool, file: PathBuf) -> std::io::Result<()> {
    install();
    if on {
        if let Some(d) = file.parent() {
            std::fs::create_dir_all(d)?;
        }
        let mut f = std::fs::OpenOptions::new().create(true).append(true).open(&file)?;
        writeln!(f, "{} INFO  lumiere-motion: logging enabled ({} {})", unix_secs(), std::env::consts::OS, env!("CARGO_PKG_VERSION"))?;
    }
    if let Ok(mut st) = STATE.lock() {
        st.file = Some(file);
    }
    ENABLED.store(on, Ordering::Relaxed);
    Ok(())
}

pub fn is_enabled() -> bool {
    ENABLED.load(Ordering::Relaxed)
}

/// The current log file (set once logging was enabled).
pub fn current_file() -> Option<PathBuf> {
    STATE.lock().ok().and_then(|s| s.file.clone())
}

/// Recent warnings and errors, oldest first.
pub fn recent() -> Vec<String> {
    STATE.lock().map(|s| s.recent.iter().cloned().collect()).unwrap_or_default()
}
