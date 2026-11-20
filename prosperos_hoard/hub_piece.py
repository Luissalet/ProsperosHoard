"""Import a registered order file through the Hub's family media route."""

from __future__ import annotations

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from . import engine
from .hoard_link._hubclient import fetch, hub_url
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


def _hub_write(store: Store, op: str, arguments: dict) -> dict:
    try:
        token = (store.data_dir / "mcp-token").read_text(encoding="utf-8-sig").strip()
    except OSError:
        token = ""
    if not token:
        raise engine.EngineError("hub_token_missing", "Prospero needs its family token to write order pieces")
    code, result = fetch(f"{hub_url()}/api/agora/{op}", arguments,
                         headers={"Authorization": f"Bearer {token}"}, timeout=30)
    if code != 200 or not isinstance(result, dict) or not result.get("ok"):
        detail = result.get("error") if isinstance(result, dict) else None
        raise engine.EngineError("hub_piece_write_failed", detail or f"The Hub could not complete {op} (HTTP {code})")
    return result


def register_piece(store: Store, asset_id: str, *, space: str, order_id: int, title: str,
                   kind: str | None = None, task_id: int | None = None,
                   derives_from: list | None = None, agent: str = "prospero") -> dict:
    asset = store.get_asset(asset_id)
    path = (store.data_dir / asset["file_path"]).resolve()
    if not path.is_relative_to(store.data_dir.resolve()) or not path.is_file():
        raise engine.EngineError("asset_file_missing", "The asset file is unavailable / Falta el archivo del recurso")
    recipe = asset.get("recipe") or {}
    model = (recipe.get("params") or {}).get("unet_name") or recipe.get("template") or ""
    result = _hub_write(store, "piece_add", {
        "space_id": space, "agent": agent, "order_id": order_id, "task_id": task_id,
        "title": title, "kind": kind or asset["kind"], "path": str(path),
        "app": "prospero", "object": asset_id, "place": asset["project_id"],
        "model": model, "derives_from": derives_from or [],
    })
    piece = result["piece"]
    undo = result.get("undo")
    return {"piece_id": piece["id"], "order_id": order_id, "asset_id": asset_id, "sha256": piece["sha256"],
            "existing": bool(result.get("existing")), "undo": {"tool": "hub_agora_order_undo",
            "arguments": {"space_id": space, "agent": agent, "token": undo}} if undo else None}


def undo_registration(store: Store, arguments: dict) -> dict:
    return _hub_write(store, "order_undo", arguments)


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
