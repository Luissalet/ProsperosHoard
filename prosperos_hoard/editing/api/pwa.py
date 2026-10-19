"""PWA manifest and service worker: makes the app installable on the phone
home screen. The service worker only caches built /assets/ files
(cache-first); every /api/ request always goes to the network. No offline
page. The guard already lets same-origin GETs through.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Response

from .. import __version__

router = APIRouter()

_MANIFEST = {
    "name": "Prospero's Hoard · Montaje",
    "short_name": "Prospero",
    "start_url": "/editing/",
    "display": "standalone",
    "background_color": "#1c141c",
    "theme_color": "#241b25",
    "lang": "es",
    "icons": [
        {"src": "/studio-assets/app-icon.png", "sizes": "any", "type": "image/png", "purpose": "any"},
    ],
}


def _service_worker() -> str:
    cache_name = json.dumps(f"prospero-editing-assets-v{__version__}")
    return f"""// Minimal service worker: installability plus a cache-first strategy for
// built /assets/ files only. API calls are always network-only and never
// cached. No offline page.
const CACHE_NAME = {cache_name};

self.addEventListener("install", () => {{
  self.skipWaiting();
}});

self.addEventListener("activate", (event) => {{
  event.waitUntil(
    (async () => {{
      const names = await caches.keys();
      await Promise.all(names.filter((name) => name.startsWith('prospero-editing-assets-') && name !== CACHE_NAME).map((name) => caches.delete(name)));
      await self.clients.claim();
    }})(),
  );
}});

self.addEventListener("fetch", (event) => {{
  if (event.request.method !== "GET") return;
  const url = new URL(event.request.url);
  if (url.pathname.startsWith("/editing/api/")) return; // network only, never cached
  if (!url.pathname.startsWith("/editing/assets/")) return; // everything else: normal browser handling

  event.respondWith(
    (async () => {{
      const cache = await caches.open(CACHE_NAME);
      const cached = await cache.match(event.request);
      if (cached) return cached;
      const response = await fetch(event.request);
      if (response.ok) cache.put(event.request, response.clone());
      return response;
    }})(),
  );
}});
"""


@router.get("/manifest.webmanifest", include_in_schema=False)
def manifest() -> Response:
    return Response(content=json.dumps(_MANIFEST), media_type="application/manifest+json")


@router.get("/sw.js", include_in_schema=False)
def service_worker() -> Response:
    return Response(content=_service_worker(), media_type="application/javascript", headers={"Service-Worker-Allowed": "/editing/"})
