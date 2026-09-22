"""Capture screenshots for the READMEs from a running --demo instance.

Usage (run from the repo root, with the demo server already running):
    PROSPERO_URL=http://127.0.0.1:8815 python3 scripts/screenshots.py

Uses the system Python's Playwright (chromium is preinstalled in this
usual setup; set PLAYWRIGHT_BROWSERS_PATH if the browsers live elsewhere);
this script deliberately does not import prosperos_hoard so it can run
outside the app's own venv.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_URL = os.environ.get("PROSPERO_URL", "http://127.0.0.1:8815")
OUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "media"


def _get_json(path: str):
    with urllib.request.urlopen(f"{BASE_URL}{path}") as resp:
        return json.loads(resp.read())


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1)

        page.goto(f"{BASE_URL}/")
        page.wait_for_timeout(500)
        page.screenshot(path=str(OUT_DIR / "01-frontend-placeholder.png"))

        page.goto(f"{BASE_URL}/api/health")
        page.wait_for_timeout(200)
        page.screenshot(path=str(OUT_DIR / "02-api-health.png"))

        page.goto(f"{BASE_URL}/api/backend")
        page.wait_for_timeout(200)
        page.screenshot(path=str(OUT_DIR / "03-api-backend.png"))

        browser.close()

    print(f"Screenshots written to {OUT_DIR}")


if __name__ == "__main__":
    main()
