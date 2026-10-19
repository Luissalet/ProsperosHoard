"""Process-level configuration read from the environment (never from the DB)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .guard import parse_allowed_hosts

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PORT = 5198
MAX_UPLOAD_BYTES = 8 * 1024 * 1024 * 1024  # uploads from the browser; local files are imported by path without copying


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(raw: str, default: int, low: int, high: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if low <= value <= high else default


@dataclass
class Config:
    """Everything the process needs before the database exists."""

    data_dir: Path = field(default_factory=lambda: REPO_ROOT / "data" / "editing")
    port: int = DEFAULT_PORT
    port_strict: bool = False
    allowed_hosts: tuple[str, ...] = ()
    data_dir_configured: bool = False
    file_roots: tuple[Path, ...] = ()  # LUMIERE_FILE_ROOTS: when set, imported files and export folders must live under one of them
    ffmpeg: str = ""  # LUMIERE_FFMPEG: explicit ffmpeg binary
    ffprobe: str = ""  # LUMIERE_FFPROBE
    workers: int = 2  # LUMIERE_WORKERS: background jobs that run at the same time (proxies, analysis)
    render_workers: int = 3  # LUMIERE_RENDER_WORKERS: chunks of one render encoded at the same time
    encoder: str = "auto"  # LUMIERE_ENCODER: auto | nvenc | x264
    max_upload_bytes: int = MAX_UPLOAD_BYTES
    base_path: str = ''

    @property
    def db_path(self) -> Path:
        return self.data_dir / "lumiere.db"

    @property
    def token_path(self) -> Path:
        return self.data_dir / "mcp-token"

    @property
    def url_path(self) -> Path:
        return self.data_dir / "url"

    @property
    def backend_json_path(self) -> Path:
        return self.data_dir / "backend.json"

    @property
    def cache_dir(self) -> Path:
        """Proxies, thumbnails, waveforms and analysis per media (safe to delete: rebuilt on demand)."""
        return self.data_dir / "cache"

    @property
    def models_dir(self) -> Path:
        """Small model files fetched on first use (the face detector); safe to delete, fetched again when needed."""
        return self.data_dir / "models"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def renders_dir(self) -> Path:
        return self.data_dir / "renders"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @classmethod
    def from_env(cls) -> "Config":
        raw_dir = _env("LUMIERE_DATA_DIR")
        port = _int(_env("LUMIERE_PORT") or _env("PORT") or str(DEFAULT_PORT), DEFAULT_PORT, 1, 65535)
        roots = tuple(Path(p).expanduser() for p in _env("LUMIERE_FILE_ROOTS").split(os.pathsep) if p.strip())
        encoder = _env("LUMIERE_ENCODER", "auto").lower()
        return cls(
            data_dir=Path(raw_dir).expanduser() if raw_dir else REPO_ROOT / "data" / "editing",
            port=port,
            port_strict=_env("PORT_STRICT") == "1",
            allowed_hosts=parse_allowed_hosts(_env("LUMIERE_ALLOWED_HOSTS")),
            data_dir_configured=bool(raw_dir),
            file_roots=roots,
            ffmpeg=_env("LUMIERE_FFMPEG"),
            ffprobe=_env("LUMIERE_FFPROBE"),
            workers=_int(_env("LUMIERE_WORKERS") or "2", 2, 1, 16),
            render_workers=_int(_env("LUMIERE_RENDER_WORKERS") or "3", 3, 1, 16),
            encoder=encoder if encoder in ("auto", "nvenc", "x264") else "auto",
        )
