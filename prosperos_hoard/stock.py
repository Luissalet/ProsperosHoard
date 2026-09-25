"""Stock footage: search Pexels and Pixabay for videos and photos, download
the best-fitting file and import it with its credit.

Both services are free with an API key (Pexels: pexels.com/api, Pixabay:
pixabay.com/api/docs) and both allow commercial use without attribution,
though they ask for it; every imported asset keeps the provider, the
author, the page URL and the licence in its recipe
(`operation: "stock"`), and `credits()` turns a list of assets into the
lines a video description needs.

Keys live in `data/backend.json -> stock` (`{"pexels": "...", "pixabay":
"..."}`, set from Settings or `PUT /api/backend/stock`) or in the
`PEXELS_API_KEY` / `PIXABAY_API_KEY` environment variables. Nothing here
talks to the network unless a key is configured.

Pure logic plus httpx: the HTTP client is injectable, so the tests run
against `httpx.MockTransport` without touching either service.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from . import engine
from .store import Store
from .util import now_iso

PROVIDERS = ("pexels", "pixabay")
KINDS = ("video", "image")
ORIENTATIONS = ("portrait", "landscape", "square", "any")
ENV_KEYS = {"pexels": "PEXELS_API_KEY", "pixabay": "PIXABAY_API_KEY"}
LICENSES = {
    "pexels": "Pexels License (free to use, attribution appreciated) - https://www.pexels.com/license/",
    "pixabay": "Pixabay Content License (free to use, attribution appreciated) - https://pixabay.com/service/license-summary/",
}
SEARCH_TIMEOUT_S = 20.0
DOWNLOAD_TIMEOUT_S = 180.0
MAX_DOWNLOAD_BYTES = 400 * 1024 * 1024


class StockError(engine.EngineError):
    pass


def aspect_orientation(aspect: Optional[str]) -> str:
    return {"9:16": "portrait", "16:9": "landscape", "1:1": "square"}.get(aspect or "", "any")


def configured_keys(raw: Optional[dict[str, Any]], env: Optional[dict[str, str]] = None) -> dict[str, str]:
    """{provider: key} from backend.json's `stock` block, then the env."""
    env = os.environ if env is None else env
    raw = raw if isinstance(raw, dict) else {}
    out: dict[str, str] = {}
    for provider in PROVIDERS:
        key = str(raw.get(provider) or env.get(ENV_KEYS[provider]) or "").strip()
        if key:
            out[provider] = key
    return out


def status(keys: dict[str, str]) -> dict[str, Any]:
    """What Settings shows: which providers are ready, never the keys."""
    return {"providers": {p: {"configured": p in keys, "key_hint": f"...{keys[p][-4:]}" if p in keys else None,
                              "get_key": {"pexels": "https://www.pexels.com/api/",
                                          "pixabay": "https://pixabay.com/api/docs/"}[p]} for p in PROVIDERS}}


# ------------------------------------------------------------------ search

def _pexels_search(client: httpx.Client, key: str, query: str, kind: str, orientation: str, per_page: int,
                   page: int) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"query": query, "per_page": per_page, "page": page}
    if orientation != "any":
        params["orientation"] = orientation
    url = "https://api.pexels.com/videos/search" if kind == "video" else "https://api.pexels.com/v1/search"
    r = client.get(url, params=params, headers={"Authorization": key}, timeout=SEARCH_TIMEOUT_S)
    _raise_for(r, "pexels")
    data = r.json()
    out = []
    if kind == "video":
        for v in data.get("videos") or []:
            files = [{"url": f.get("link"), "width": f.get("width") or 0, "height": f.get("height") or 0,
                      "quality": f.get("quality")}
                     for f in v.get("video_files") or [] if f.get("link") and (f.get("file_type") or "video/mp4") == "video/mp4"]
            if not files:
                continue
            user = v.get("user") or {}
            out.append({"provider": "pexels", "id": str(v.get("id")), "kind": "video", "width": v.get("width") or 0,
                        "height": v.get("height") or 0, "duration_s": float(v.get("duration") or 0), "page_url": v.get("url"),
                        "author": user.get("name"), "author_url": user.get("url"), "preview_url": v.get("image"),
                        "files": files, "tags": []})
    else:
        for p in data.get("photos") or []:
            src = p.get("src") or {}
            files = [{"url": src[k], "width": w, "height": h} for k, w, h in (
                ("original", p.get("width") or 0, p.get("height") or 0),
                ("large2x", min(p.get("width") or 0, 1880), 0)) if src.get(k)]
            if not files:
                continue
            out.append({"provider": "pexels", "id": str(p.get("id")), "kind": "image", "width": p.get("width") or 0,
                        "height": p.get("height") or 0, "duration_s": 0.0, "page_url": p.get("url"),
                        "author": p.get("photographer"), "author_url": p.get("photographer_url"),
                        "preview_url": src.get("medium") or src.get("small"), "files": files[:1],
                        "tags": [t for t in re.split(r"[\s,]+", p.get("alt") or "") if t][:8]})
    return out


def _pixabay_search(client: httpx.Client, key: str, query: str, kind: str, orientation: str, per_page: int,
                    page: int) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"key": key, "q": query[:100], "per_page": max(3, per_page), "page": page, "safesearch": "true"}
    url = "https://pixabay.com/api/videos/" if kind == "video" else "https://pixabay.com/api/"
    if kind == "image":
        params["image_type"] = "photo"
        if orientation in ("portrait", "landscape"):
            params["orientation"] = "vertical" if orientation == "portrait" else "horizontal"
    r = client.get(url, params=params, timeout=SEARCH_TIMEOUT_S)
    _raise_for(r, "pixabay")
    data = r.json()
    out = []
    for h in data.get("hits") or []:
        tags = [t.strip() for t in str(h.get("tags") or "").split(",") if t.strip()][:8]
        common = {"provider": "pixabay", "id": str(h.get("id")), "page_url": h.get("pageURL"), "author": h.get("user"),
                  "author_url": f"https://pixabay.com/users/{h.get('user')}-{h.get('user_id')}/" if h.get("user") else None,
                  "tags": tags}
        if kind == "video":
            files = [{"url": f.get("url"), "width": f.get("width") or 0, "height": f.get("height") or 0, "quality": name}
                     for name, f in (h.get("videos") or {}).items() if isinstance(f, dict) and f.get("url")]
            if not files:
                continue
            biggest = max(files, key=lambda f: f["width"] * f["height"])
            out.append({**common, "kind": "video", "width": biggest["width"], "height": biggest["height"],
                        "duration_s": float(h.get("duration") or 0), "preview_url": (h.get("videos") or {}).get("tiny", {}).get("thumbnail"),
                        "files": files})
        else:
            url_ = h.get("largeImageURL") or h.get("webformatURL")
            if not url_:
                continue
            out.append({**common, "kind": "image", "width": h.get("imageWidth") or 0, "height": h.get("imageHeight") or 0,
                        "duration_s": 0.0, "preview_url": h.get("previewURL") or h.get("webformatURL"),
                        "files": [{"url": url_, "width": h.get("imageWidth") or 0, "height": h.get("imageHeight") or 0}]})
    return out


def _raise_for(r: httpx.Response, provider: str) -> None:
    if r.status_code in (401, 403):
        raise StockError("stock_key_rejected", f"{provider} rejected the API key (HTTP {r.status_code}); check it in Settings")
    if r.status_code == 429:
        raise StockError("stock_rate_limited", f"{provider} rate limit reached; try again in a while")
    if r.status_code >= 400:
        raise StockError("stock_search_failed", f"{provider} answered HTTP {r.status_code}")


_SEARCHERS: dict[str, Callable[..., list[dict[str, Any]]]] = {"pexels": _pexels_search, "pixabay": _pixabay_search}


def _orientation_ok(item: dict[str, Any], orientation: str) -> bool:
    w, h = item.get("width") or 0, item.get("height") or 0
    if orientation == "any" or not w or not h:
        return True
    ratio = w / h
    return {"portrait": ratio < 0.9, "landscape": ratio > 1.1, "square": 0.8 <= ratio <= 1.25}[orientation]


def search(keys: dict[str, str], query: str, kind: str = "video", orientation: str = "portrait",
           providers: Optional[list[str]] = None, per_page: int = 12, page: int = 1, min_duration_s: float = 0.0,
           exclude: Optional[set[str]] = None, client: Optional[httpx.Client] = None) -> dict[str, Any]:
    """Search every configured provider (in the given order) and return
    `{"items": [...], "providers": [...], "errors": {...}}`. Items carry
    `ref` = "<provider>:<id>" for `fetch`. A provider that fails is reported
    in `errors`; the others still answer."""
    query = str(query or "").strip()
    if not query:
        raise StockError("empty_query", "give a search query (a few English keywords work best)")
    if kind not in KINDS:
        raise StockError("bad_kind", f"kind must be one of {', '.join(KINDS)}")
    if orientation not in ORIENTATIONS:
        raise StockError("bad_orientation", f"orientation must be one of {', '.join(ORIENTATIONS)}")
    wanted = [p for p in (providers or list(PROVIDERS)) if p in PROVIDERS]
    ready = [p for p in wanted if p in keys]
    if not ready:
        raise StockError("stock_not_configured", "no stock provider has an API key: add a free Pexels or Pixabay key "
                                                 "in Settings > Stock footage (or PEXELS_API_KEY / PIXABAY_API_KEY)")
    per_page = max(1, min(40, int(per_page)))
    own = client is None
    client = client or httpx.Client(follow_redirects=True)
    items: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    try:
        for provider in ready:
            try:
                found = _SEARCHERS[provider](client, keys[provider], query, kind, orientation, per_page, page)
            except StockError as exc:
                errors[provider] = exc.message if hasattr(exc, "message") else str(exc)
                continue
            except httpx.HTTPError as exc:
                errors[provider] = f"{type(exc).__name__}: {exc}"[:200]
                continue
            for item in found:
                item["ref"] = f"{item['provider']}:{item['id']}"
                if exclude and item["ref"] in exclude:
                    continue
                if kind == "video" and min_duration_s and item["duration_s"] and item["duration_s"] < min_duration_s:
                    continue
                if not _orientation_ok(item, orientation):
                    continue
                items.append(item)
    finally:
        if own:
            client.close()
    if not items and len(errors) == len(ready):
        raise StockError("stock_search_failed", "; ".join(f"{p}: {m}" for p, m in errors.items()))
    return {"items": items, "providers": ready, "errors": errors, "query": query}


def pick_file(item: dict[str, Any], short_side: int = 1080) -> dict[str, Any]:
    """The smallest file whose short side reaches `short_side` (so a 1080p
    render never downloads 4K), else the biggest one."""
    files = [f for f in item.get("files") or [] if f.get("url")]
    if not files:
        raise StockError("stock_no_file", f"{item.get('ref')} has no downloadable file")

    def short(f: dict[str, Any]) -> int:
        w, h = f.get("width") or 0, f.get("height") or 0
        return min(w, h) if w and h else max(w, h)

    big_enough = [f for f in files if short(f) >= short_side]
    if big_enough:
        return min(big_enough, key=lambda f: (short(f), f.get("width", 0) * f.get("height", 0)))
    return max(files, key=lambda f: short(f))


def _download(client: httpx.Client, url: str, dest: Path, should_cancel: Optional[Callable[[], bool]] = None) -> None:
    size = 0
    with client.stream("GET", url, timeout=DOWNLOAD_TIMEOUT_S) as r:
        if r.status_code >= 400:
            raise StockError("stock_download_failed", f"download answered HTTP {r.status_code}")
        with dest.open("wb") as fh:
            for chunk in r.iter_bytes(1 << 16):
                size += len(chunk)
                if size > MAX_DOWNLOAD_BYTES:
                    raise StockError("too_large", "the stock file is over 400 MB")
                if should_cancel and should_cancel():
                    raise StockError("cancelled", "cancelled")
                fh.write(chunk)


def _ext_for(url: str, kind: str) -> str:
    ext = Path(url.split("?")[0]).suffix.lower()
    if kind == "video":
        return ext if ext in (".mp4", ".mov", ".webm") else ".mp4"
    return ext if ext in (".jpg", ".jpeg", ".png", ".webp") else ".jpg"


def fetch(store: Store, project_id: str, item: dict[str, Any], short_side: int = 1080, query: Optional[str] = None,
          client: Optional[httpx.Client] = None, should_cancel: Optional[Callable[[], bool]] = None) -> dict[str, Any]:
    """Download one search result into the project as an asset whose recipe
    carries the credit (`operation: "stock"`)."""
    chosen = pick_file(item, short_side)
    own = client is None
    client = client or httpx.Client(follow_redirects=True)
    tmp_dir = store.data_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=tmp_dir) as work:
        dest = Path(work) / f"{item['provider']}_{item['id']}{_ext_for(chosen['url'], item['kind'])}"
        try:
            _download(client, chosen["url"], dest, should_cancel)
        except httpx.HTTPError as exc:
            raise StockError("stock_download_failed", f"{type(exc).__name__}: {exc}"[:300]) from None
        finally:
            if own:
                client.close()
        recipe = {"operation": "stock", "backend": "stock", "provider": item["provider"], "stock_id": item["id"],
                  "ref": item.get("ref") or f"{item['provider']}:{item['id']}", "page_url": item.get("page_url"),
                  "author": item.get("author"), "author_url": item.get("author_url"), "license": LICENSES[item["provider"]],
                  "file_width": chosen.get("width"), "file_height": chosen.get("height"), "query": query,
                  "created_at": now_iso()}
        name = f"{item['provider'].capitalize()} {item['id']}" + (f" - {item['author']}" if item.get("author") else "")
        asset = engine.import_asset(store, project_id, dest, kind_hint=item["kind"], original_name=dest.name, recipe=recipe,
                                    tags=["stock", item["provider"]] + [t.lower()[:40] for t in (item.get("tags") or [])[:5]],
                                    source="import")
    return store.update_asset(asset["id"], name=name)


def credits(assets: list[dict[str, Any]]) -> list[str]:
    """One credit line per distinct stock asset, for a video description."""
    lines, seen = [], set()
    for a in assets:
        r = a.get("recipe") or {}
        if r.get("operation") != "stock" or r.get("ref") in seen:
            continue
        seen.add(r.get("ref"))
        who = r.get("author") or "unknown author"
        lines.append(f"{'Video' if a.get('kind') == 'video' else 'Photo'} by {who} on {str(r.get('provider')).capitalize()}"
                     + (f" - {r['page_url']}" if r.get("page_url") else ""))
    return lines


def compact_item(item: dict[str, Any]) -> dict[str, Any]:
    return {"ref": item["ref"], "kind": item["kind"], "width": item.get("width"), "height": item.get("height"),
            "duration_s": item.get("duration_s") or None, "author": item.get("author"), "page_url": item.get("page_url"),
            "preview_url": item.get("preview_url"), "tags": item.get("tags") or []}
