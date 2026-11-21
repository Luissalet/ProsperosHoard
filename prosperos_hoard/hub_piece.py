"""Import a registered order file through the Hub's family media route."""

from __future__ import annotations

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode

import httpx

from . import engine
from .hoard_link._hubclient import fetch, hub_url
from .store import NotFound, Store


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
_MAX_ORDER_MATERIALS = 200


def _hub_headers(store: Store) -> dict:
    try:
        token = (store.data_dir / "mcp-token").read_text(encoding="utf-8-sig").strip()
    except OSError:
        token = ""
    if not token:
        raise engine.EngineError("hub_token_missing", "Prospero needs its family token to write order pieces")
    return {"Authorization": f"Bearer {token}"}


def _hub_write(store: Store, op: str, arguments: dict) -> dict:
    code, result = fetch(f"{hub_url()}/api/agora/{op}", arguments,
                         headers=_hub_headers(store), timeout=30)
    if code != 200 or not isinstance(result, dict) or not result.get("ok"):
        detail = result.get("error") if isinstance(result, dict) else None
        raise engine.EngineError("hub_piece_write_failed", detail or f"The Hub could not complete {op} (HTTP {code})")
    return result


def _recipe_inputs(recipe: dict) -> tuple:
    return (*(recipe.get("input_asset_ids") or ()), recipe.get("derived_from"), recipe.get("retake_of"),
            recipe.get("edit_of"))


def _imported_piece_id(pieces: dict, receipt: dict) -> int | None:
    piece = pieces.get(receipt.get("id"))
    if not piece or not receipt.get("sha256"):
        return None
    preview = receipt.get("preview")
    digest = piece.get("sha256")
    if preview is not None:
        copies = piece.get("previews_stored") or []
        digest = Path(copies[preview]).stem if 0 <= preview < len(copies) else None
    return piece["id"] if digest == receipt["sha256"] else None


def _media_sources(store: Store, asset: dict, space: str, order_id: int, declared: list) -> list:
    """Resolve recipe inputs against registered pieces of this order."""
    recipe = asset.get("recipe") or {}
    if asset["kind"] != "video" and recipe.get("operation") not in ("frame", "extract_frame"):
        return declared
    if not (recipe.get("hub_piece") or any(_recipe_inputs(recipe))):
        return declared
    query = urlencode({"space_id": space, "order_id": order_id})
    code, result = fetch(f"{hub_url()}/api/agora/order?{query}", headers=_hub_headers(store), timeout=30)
    if code != 200 or not isinstance(result, dict) or not result.get("ok"):
        raise engine.EngineError("hub_piece_read_failed", f"The Hub could not read order #{order_id} (HTTP {code})")
    pieces = result["order"]["piece_list"]
    by_id = {p["id"]: p for p in pieces}
    superseded = {p.get("supersedes") for p in pieces}
    by_asset: dict[str, list[int]] = {}
    for piece in pieces:
        if piece["app"] == "prospero" and piece["id"] not in superseded:
            by_asset.setdefault(piece["object"], []).append(piece["id"])
    known = {(d.get("piece") or d.get("piece_id")) if isinstance(d, dict) else d for d in declared}
    sources = list(declared)
    seen = set()

    def visit(current: dict, *, output: bool = False) -> None:
        if (len(sources) >= _MAX_ORDER_MATERIALS
                or current["id"] in seen or current["kind"] not in ("image", "video")):
            return
        seen.add(current["id"])
        data = current.get("recipe") or {}
        imported = _imported_piece_id(by_id, data.get("hub_piece") or {})
        matches = [] if output else by_asset.get(current["id"], [])
        piece_id = imported if imported is not None else matches[0] if len(matches) == 1 else None
        if piece_id is not None:
            if piece_id not in known:
                sources.append({"piece": piece_id, "role": "material"})
                known.add(piece_id)
            return
        if matches:  # The order contains several pieces for the same input asset.
            return
        for source_id in _recipe_inputs(data):
            if source_id:
                try:
                    source = store.get_asset(source_id)
                except NotFound:
                    continue
                visit(source)

    visit(asset, output=True)
    return sources


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
        "model": model, "derives_from": _media_sources(store, asset, space, order_id, derives_from or []),
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
