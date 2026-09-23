"""SQLite schema and connection handling.

Plain stdlib sqlite3, WAL mode, row factory returns dict-like rows. No
FastAPI or pydantic imports here - this module is pure persistence and is
safe to import from the MCP adapter's test harness or a script.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 5

# Columns added after v1: (table, column, declaration). Applied with ALTER
# TABLE on databases created by an older version.
_ADDED_COLUMNS = [
    ("assets", "name", "TEXT"),
    ("assets", "analysis_json", "TEXT"),
    ("jobs", "cancel_requested", "INTEGER NOT NULL DEFAULT 0"),
    ("timelines", "finishing_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("projects", "image_engine", "TEXT NOT NULL DEFAULT 'auto'"),
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    brief TEXT,
    cover_asset_id TEXT,
    image_engine TEXT NOT NULL DEFAULT 'auto',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('image','video','audio','font','lyrics','layout')),
    file_path TEXT NOT NULL,
    mime TEXT,
    width INTEGER,
    height INTEGER,
    duration_s REAL,
    thumb_path TEXT,
    waveform_json TEXT,
    tags_json TEXT NOT NULL DEFAULT '[]',
    rating INTEGER NOT NULL DEFAULT 0,
    favourite INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    source TEXT NOT NULL CHECK (source IN ('import','generated','derived','rendered')),
    recipe_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assets_project ON assets(project_id);
CREATE INDEX IF NOT EXISTS idx_assets_kind ON assets(kind);

CREATE TABLE IF NOT EXISTS characters (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    role TEXT,
    bio TEXT,
    prompt TEXT,
    negative TEXT,
    palette_json TEXT NOT NULL DEFAULT '[]',
    reference_asset_ids_json TEXT NOT NULL DEFAULT '[]',
    canonical_asset_id TEXT,
    voice_json TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_characters_project ON characters(project_id);

CREATE TABLE IF NOT EXISTS groups (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    concept TEXT,
    member_ids_json TEXT NOT NULL DEFAULT '[]',
    logo_asset_id TEXT,
    colours_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_groups_project ON groups(project_id);

CREATE TABLE IF NOT EXISTS style_presets (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    name TEXT NOT NULL,
    prompt_prefix TEXT,
    prompt_suffix TEXT,
    negative TEXT,
    defaults_json TEXT NOT NULL DEFAULT '{}',
    notes TEXT,
    is_builtin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS boards (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'moodboard',
    items_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_boards_project ON boards(project_id);

CREATE TABLE IF NOT EXISTS timelines (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    aspect TEXT NOT NULL DEFAULT '9:16',
    fps INTEGER NOT NULL DEFAULT 30,
    width INTEGER NOT NULL DEFAULT 1080,
    height INTEGER NOT NULL DEFAULT 1920,
    audio_asset_id TEXT,
    tracks_json TEXT NOT NULL DEFAULT '[]',
    finishing_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_timelines_project ON timelines(project_id);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    type TEXT NOT NULL,
    lane TEXT NOT NULL CHECK (lane IN ('gpu','cpu')),
    params_json TEXT NOT NULL DEFAULT '{}',
    inputs_json TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued','waiting_gpu','running','done','failed','cancelled')),
    progress REAL NOT NULL DEFAULT 0.0,
    message TEXT,
    outputs_json TEXT,
    backend TEXT,
    log_excerpt TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_jobs_lane ON jobs(lane);

CREATE TABLE IF NOT EXISTS studio_voices (
    id TEXT PRIMARY KEY,
    project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    engine_id TEXT NOT NULL,
    voice_ref TEXT,
    sample_path TEXT,
    language TEXT,
    cloned INTEGER NOT NULL DEFAULT 0,
    reference_transcript TEXT,
    quality_json TEXT NOT NULL DEFAULT '{}',
    presets_json TEXT NOT NULL DEFAULT '[]',
    tags_json TEXT NOT NULL DEFAULT '[]',
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_studio_voices_project ON studio_voices(project_id);

CREATE TABLE IF NOT EXISTS agent_calls (
    id TEXT PRIMARY KEY,
    tool TEXT NOT NULL,
    args_summary TEXT,
    duration_ms REAL,
    ok INTEGER NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_calls_created ON agent_calls(created_at);
"""


_local = threading.local()


def connect(data_dir: Path) -> sqlite3.Connection:
    """Open (and, on first use, create) the app's SQLite database.

    One connection per thread (sqlite3 connections must not be shared
    between threads); callers get a cached connection for the calling
    thread, keyed by the database folder.
    """
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    key = str(data_dir.resolve())
    cache: dict[str, sqlite3.Connection] = getattr(_local, "conns", None) or {}
    if key in cache:
        return cache[key]

    db_path = data_dir / "prosperos.sqlite3"
    conn = sqlite3.connect(str(db_path), timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    for table, column, decl in _ADDED_COLUMNS:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in have:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            except sqlite3.OperationalError as exc:  # another thread added it first
                if "duplicate column" not in str(exc):
                    raise
    conn.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version', ?)", (str(SCHEMA_VERSION),))
    conn.commit()
    cache[key] = conn
    _local.conns = cache
    return conn


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def loads(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
