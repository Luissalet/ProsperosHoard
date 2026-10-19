"""SQLite connection (WAL) and ordered schema migrations."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

MIGRATIONS: list[str] = [
    # 1: settings, media + analysis cache, projects + history, jobs, renders, plans
    """
    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE media (
      id TEXT PRIMARY KEY,
      path TEXT NOT NULL,
      name TEXT NOT NULL,
      kind TEXT NOT NULL,              -- video | audio | image
      origin TEXT NOT NULL DEFAULT 'import',  -- import | upload | render | frame
      fingerprint TEXT NOT NULL,
      bytes INTEGER NOT NULL DEFAULT 0,
      duration_ms INTEGER NOT NULL DEFAULT 0,
      width INTEGER NOT NULL DEFAULT 0,
      height INTEGER NOT NULL DEFAULT 0,
      fps REAL NOT NULL DEFAULT 0,
      has_video INTEGER NOT NULL DEFAULT 0,
      has_audio INTEGER NOT NULL DEFAULT 0,
      probe TEXT NOT NULL DEFAULT '{}',
      proxy TEXT NOT NULL DEFAULT 'pending',   -- pending | ready | failed | none
      tags TEXT NOT NULL DEFAULT '[]',
      created_ts REAL NOT NULL
    );
    CREATE UNIQUE INDEX media_fp ON media(fingerprint, path);
    CREATE TABLE analysis (
      media_id TEXT NOT NULL REFERENCES media(id) ON DELETE CASCADE,
      kind TEXT NOT NULL,              -- waveform | silences | scenes | loudness | beats | transcript | motion | faces
      params TEXT NOT NULL DEFAULT '{}',
      result TEXT NOT NULL,
      updated_ts REAL NOT NULL,
      PRIMARY KEY (media_id, kind)
    );
    CREATE TABLE projects (
      id TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      doc TEXT NOT NULL,
      rev INTEGER NOT NULL DEFAULT 1,
      head INTEGER NOT NULL DEFAULT 1,
      is_template INTEGER NOT NULL DEFAULT 0,
      created_ts REAL NOT NULL,
      updated_ts REAL NOT NULL
    );
    CREATE TABLE history (
      project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
      seq INTEGER NOT NULL,
      label TEXT NOT NULL,
      actor TEXT NOT NULL DEFAULT 'ui',
      doc TEXT NOT NULL,
      ts REAL NOT NULL,
      PRIMARY KEY (project_id, seq)
    );
    CREATE TABLE jobs (
      id TEXT PRIMARY KEY,
      kind TEXT NOT NULL,
      label TEXT NOT NULL DEFAULT '',
      state TEXT NOT NULL DEFAULT 'queued',   -- queued | running | done | failed | canceled
      progress REAL NOT NULL DEFAULT 0,
      detail TEXT NOT NULL DEFAULT '',
      media_id TEXT,
      project_id TEXT,
      params TEXT NOT NULL DEFAULT '{}',
      result TEXT NOT NULL DEFAULT '{}',
      error TEXT NOT NULL DEFAULT '',
      created_ts REAL NOT NULL,
      started_ts REAL,
      finished_ts REAL
    );
    CREATE INDEX jobs_state ON jobs(state, created_ts);
    CREATE TABLE renders (
      id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL,
      job_id TEXT,
      preset TEXT NOT NULL,
      mode TEXT NOT NULL DEFAULT 'final',
      path TEXT NOT NULL,
      bytes INTEGER NOT NULL DEFAULT 0,
      duration_ms INTEGER NOT NULL DEFAULT 0,
      width INTEGER NOT NULL DEFAULT 0,
      height INTEGER NOT NULL DEFAULT 0,
      qc TEXT NOT NULL DEFAULT '{}',
      created_ts REAL NOT NULL
    );
    CREATE INDEX renders_project ON renders(project_id, created_ts);
    CREATE TABLE plans (
      id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
      instruction TEXT NOT NULL,
      steps TEXT NOT NULL,
      source TEXT NOT NULL,              -- model | rules
      notes TEXT NOT NULL DEFAULT '',
      state TEXT NOT NULL DEFAULT 'draft',  -- draft | applied | discarded
      created_ts REAL NOT NULL,
      applied_ts REAL
    );
    """,
    # 2: translated subtitles per project and language; which output of a multi-format export a render is
    """
    CREATE TABLE translations (
      project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
      language TEXT NOT NULL,
      signature TEXT NOT NULL,           -- fingerprint of the source cues (text and timing) this translation answers
      data TEXT NOT NULL,
      updated_ts REAL NOT NULL,
      PRIMARY KEY (project_id, language)
    );
    ALTER TABLE renders ADD COLUMN variant TEXT NOT NULL DEFAULT '';
    """,
    # 3: durable receipts for explicitly keyed timeline edits
    """
    CREATE TABLE edit_receipts (
      project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
      request_id TEXT NOT NULL,
      digest TEXT NOT NULL,
      result TEXT NOT NULL,
      created_ts REAL NOT NULL,
      PRIMARY KEY (project_id, request_id)
    );
    """,
]


class Database:
    """One connection shared by every thread, guarded by a re-entrant lock. The MCP bridge never opens this file."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    def migrate(self) -> None:
        with self.lock:
            self.conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
            row = self.conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            current = row["v"] or 0
            for index, sql in enumerate(MIGRATIONS, start=1):
                if index <= current:
                    continue
                script = f"BEGIN;\n{sql}\nINSERT INTO schema_version(version) VALUES ({index});\nCOMMIT;"
                try:
                    self.conn.executescript(script)
                except Exception:
                    if self.conn.in_transaction:
                        self.conn.execute("ROLLBACK")
                    raise

    def schema_version(self) -> int:
        row = self.one("SELECT MAX(version) AS v FROM schema_version")
        return int(row["v"] or 0)

    def query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, params)

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def transaction(self):
        """`with db.transaction():` — BEGIN IMMEDIATE / COMMIT (ROLLBACK on error) under the lock."""
        return _Transaction(self)

    def close(self) -> None:
        with self.lock:
            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self.conn.close()


class _Transaction:
    def __init__(self, db: Database):
        self.db = db

    def __enter__(self):
        self.db.lock.acquire()
        try:
            self.db.conn.execute("BEGIN IMMEDIATE")
        except Exception:
            self.db.lock.release()
            raise
        return self.db.conn

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.db.conn.execute("COMMIT")
            else:
                self.db.conn.execute("ROLLBACK")
        finally:
            self.db.lock.release()
