"""Import a registered order file through the Hub's family media route."""

from __future__ import annotations

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from . import engine
from .hoard_link._hubclient import hub_url
from .store import Store


# The route supplies the MIME type of the stored file. Pick a suffix the normal
# studio importer understands without accepting a filename/path from the peer.
_EXTENSIONS = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/bmp": ".bmp",
    "image/x-ms-bmp": ".bmp",
    "video/mp4": ".mp4", "video/quicktime": ".mov", "video/x-matroska": ".mkv", "video/webm": ".webm",
    "image/gif": ".gif", "audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav",
    "audio/flac": ".flac", "audio/x-flac": ".flac", "audio/ogg": ".ogg", "application/ogg": ".ogg", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a",
    "text/plain": ".txt", "font/ttf": ".ttf", "font/otf": ".otf",
    "application/x-font-ttf": ".ttf", "application/x-font-opentype": ".otf",
}


def import_piece(store: Store, project_id: str, piece_id: int, *, preview: int | None = None,
                 kind_hint: str | None = None) -> dict:
    """Import an order piece (or numbered preview) and retain its byte identity."""
    store.get_project(project_id)
    try:
        token = (store.data_dir / "mcp-token").read_text(encoding="utf-8-sig").strip()
    except OSError:
        token = ""
    if not token:
        raise engine.EngineError("hub_token_missing", "Prospero needs its family token to read order pieces")

    source = f"piece {piece_id}" if preview is None else f"piece {piece_id}, preview {preview}"
    url = f"{hub_url()}/api/agora/piece_file/{piece_id}"
    query = {"preview": preview} if preview is not None else None
    digest = hashlib.sha256()
    with TemporaryDirectory(prefix="prospero-piece-") as folder:
        try:
            with httpx.stream("GET", url, params=query, headers={"Authorization": f"Bearer {token}"},
                              follow_redirects=False, trust_env=False, timeout=30.0) as response:
                if response.status_code != 200:
                    raise engine.EngineError("hub_piece_unavailable", f"the Hub could not serve {source} (HTTP {response.status_code})")
                mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                suffix = _EXTENSIONS.get(mime)
                if suffix is None:
                    raise engine.EngineError("unsupported_kind", f"{source} is not supported studio media ({mime or 'no media type'})")
                kind = engine._detect_kind(suffix, kind_hint)
                limit = {"image": engine.MAX_IMAGE_BYTES, "lyrics": engine.MAX_LYRICS_BYTES,
                         "font": engine.MAX_FONT_BYTES}.get(kind, engine.MAX_MEDIA_BYTES)
                length = response.headers.get("content-length", "")
                if length.isdecimal() and int(length) > limit:
                    raise engine.EngineError("too_large", f"{source} exceeds the studio's import limit")
                path = Path(folder) / f"{source}{suffix}"
                size = 0
                with path.open("wb") as output:
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > limit:
                            raise engine.EngineError("too_large", f"{source} exceeds the studio's import limit")
                        output.write(chunk)
                        digest.update(chunk)
        except httpx.HTTPError as exc:
            raise engine.EngineError("hub_unreachable", f"the Hub could not transfer {source}") from exc

        origin = {"id": piece_id, "sha256": digest.hexdigest()}
        if preview is not None:
            origin["preview"] = preview
        return engine.import_asset(store, project_id, path, kind, recipe={"hub_piece": origin})
