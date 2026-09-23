"""Capture the README screenshots from a fresh --demo instance.

Usage (from the repo root, after `npm run build` in frontend/):
    python3 scripts/screenshots.py

Starts `python -m prosperos_hoard --demo` (the repo's .venv Python when it
exists) on a free port with a throwaway data folder, drives the real UI with
Playwright (1440x900, device scale 1, dark theme, English) and writes
optimised PNGs to docs/media/. Everything shown comes from the procedural
demo backend, which the READMEs say in every caption.

Needs Playwright with Chromium for the Python running this script (set
PLAYWRIGHT_BROWSERS_PATH if the browsers live elsewhere). Never run
"playwright install" from here.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "docs" / "media"
MAX_BYTES = 400 * 1024


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _json(url: str):
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read())


def _optimise(path: Path) -> None:
    """Keep each PNG under 400 KB: lossless first, then a 256-colour palette."""
    with Image.open(path) as im:
        im = im.convert("RGB")
        im.save(path, optimize=True)
        if path.stat().st_size > MAX_BYTES:
            im.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.FLOYDSTEINBERG).save(path, optimize=True)
    print(f"  {path.name}: {path.stat().st_size // 1024} KB")


def main() -> int:
    python = REPO / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python = python if python.exists() else Path(sys.executable)
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    data = Path(tempfile.mkdtemp(prefix="prospero-shots-"))
    log = (data / "app.log").open("w", encoding="utf-8")
    proc = subprocess.Popen([str(python), "-m", "prosperos_hoard", "--demo", "--no-browser", "--port", str(port), "--data-dir", str(data / "data")],
                            cwd=REPO, stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(240):
            try:
                if _json(f"{base}/api/health")["status"] == "ok":
                    break
            except OSError:
                time.sleep(0.5)
        else:
            raise SystemExit("the demo app did not start; see " + str(data / "app.log"))
        pid = _json(f"{base}/api/projects")["items"][0]["id"]
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1, locale="en-US",
                                      color_scheme="dark")
            page = ctx.new_page()

            # 1. Generate: @mentions expanded into the exact final prompt
            page.goto(f"{base}/#/p/{pid}/generate")
            page.wait_for_selector(".results-grid .tile")
            page.fill("textarea", "@Iris Volt and @Mika Frost backstage before the show, confetti in the air")
            page.locator("label.field:has-text('Style') select").select_option(label="Film still 35mm")
            page.wait_for_selector(".final-prompt mark")
            page.wait_for_timeout(800)
            page.screenshot(path=str(OUT_DIR / "01-generate.png"))

            # 2. Timeline: the beat-synced cut with lyrics and the rendered preview
            page.goto(f"{base}/#/p/{pid}/timeline")
            page.wait_for_selector(".clip-block")
            page.locator(".clip-block").nth(6).click()
            page.wait_for_timeout(1500)
            page.screenshot(path=str(OUT_DIR / "02-timeline.png"))

            # 3. Library lightbox on the photocard set, with its recipe
            page.goto(f"{base}/#/p/{pid}/library")
            page.wait_for_selector(".masonry .tile")
            page.fill("#library-search", "photocard set")
            page.wait_for_timeout(900)
            page.locator(".masonry .tile").first.click()
            page.wait_for_selector(".overlay .lightbox-side h2")
            page.wait_for_timeout(1200)
            page.screenshot(path=str(OUT_DIR / "03-photocards.png"))
            page.click(".lightbox-tools button:has-text('Close')")

            # 4. Audio: tempo, beats and A/B/A sections of the demo song
            page.goto(f"{base}/#/p/{pid}/audio")
            page.wait_for_selector(".wave-wrap canvas")
            page.wait_for_timeout(1500)
            page.screenshot(path=str(OUT_DIR / "04-audio.png"))
            browser.close()
        for name in ("01-generate.png", "02-timeline.png", "03-photocards.png", "04-audio.png"):
            _optimise(OUT_DIR / name)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
    print(f"Screenshots written to {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
