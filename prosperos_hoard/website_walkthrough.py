"""Record a real website journey, caption it and keep the video in the project."""
from __future__ import annotations

import math
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from . import engine, procutil
from .backend import ffmpeg_path
from .jobs import JobCancelled
from .promo_brand import open_browser
from .promo_web import _probe, sheet_from_video


def url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise engine.EngineError("bad_url", "Give an http(s) address without embedded credentials")
    return value


def plan(address: str, steps: list[dict], title: str = "", hold_s: float = 2) -> dict:
    """Validate before queueing; local apps are valid recording targets."""
    url(address)
    if not math.isfinite(hold_s) or not 0.5 <= hold_s <= 10:
        raise engine.EngineError("bad_hold", "hold_s must be between 0.5 and 10 seconds")
    if len(steps) > 24:
        raise engine.EngineError("bad_steps", "Use at most 24 steps for a short walkthrough")
    checked = []
    for step in steps:
        action = step.get("action", "wait")
        if action not in ("click", "navigate", "scroll", "wait"):
            raise engine.EngineError("bad_step", "Use click, navigate, scroll or wait")
        item = {"action": action, "caption": str(step.get("caption") or "")[:240]}
        if action == "click":
            item["selector"] = str(step.get("selector") or "").strip()
            if not item["selector"]:
                raise engine.EngineError("bad_step", "A click needs a selector")
        if action == "navigate":
            item["url"] = url(urljoin(address, str(step.get("url") or "")))
        if action == "scroll":
            try:
                item["y"] = float(step.get("y", 600))
                if not math.isfinite(item["y"]):
                    raise ValueError()
            except (TypeError, ValueError):
                raise engine.EngineError("bad_step", "A scroll needs a finite y distance")
        checked.append(item)
    return {"url": address, "steps": checked, "title": title[:240], "hold_s": hold_s}


# The caption is an overlay on the actual page; textContent keeps user text literal.
_CAPTION = """text => {
  let el = document.getElementById('__hoard_walkthrough_caption');
  if (!el) {
    el = document.createElement('div'); el.id = '__hoard_walkthrough_caption';
    Object.assign(el.style, {position:'fixed',left:'5%',bottom:'5%',maxWidth:'85%',
      padding:'14px 20px',background:'#101820f2',color:'#fff',font:'600 24px/1.4 system-ui',overflowWrap:'anywhere',
      borderRadius:'8px',zIndex:'2147483647',pointerEvents:'none',boxShadow:'0 4px 20px #0005'});
    document.documentElement.appendChild(el);
  }
  el.textContent = text; el.style.display = text ? 'block' : 'none';
}"""


def record(spec: dict, folder: Path, progress) -> tuple[Path, list[dict]]:
    """A fresh browser context records all actions, including page transitions."""
    receipts = []
    cancelled = getattr(progress, "cancelled", lambda: False)

    def hold(page):
        for _ in range(round(spec["hold_s"] * 10)):
            if cancelled():
                raise JobCancelled()
            page.wait_for_timeout(100)

    with open_browser() as browser:
        context = browser.new_context(viewport={"width": 1280, "height": 720},
                                      record_video_dir=str(folder), record_video_size={"width": 1280, "height": 720})
        page = context.new_page()
        video = page.video
        try:
            response = page.goto(spec["url"], wait_until="domcontentloaded", timeout=30000)
            if response and not response.ok:
                raise engine.EngineError("page_failed", f"The website responded HTTP {response.status}")
            page.evaluate(_CAPTION, spec["title"])
            hold(page)
            for i, step in enumerate(spec["steps"]):
                if cancelled():
                    raise JobCancelled()
                progress(0.1 + 0.55 * i / max(1, len(spec["steps"])), f"recording step {i + 1}")
                if step["action"] == "click":
                    target = page.locator(step["selector"])
                    target.hover(timeout=10000)
                    target.click(timeout=10000)
                    page.wait_for_load_state("domcontentloaded")
                elif step["action"] == "navigate":
                    response = page.goto(step["url"], wait_until="domcontentloaded", timeout=30000)
                    if response and not response.ok:
                        raise engine.EngineError("page_failed", f"The website responded HTTP {response.status}")
                elif step["action"] == "scroll":
                    page.mouse.wheel(0, step["y"])
                page.evaluate(_CAPTION, step["caption"])
                hold(page)
                receipts.append({**step, "url": page.url, "page_title": page.title()})
        finally:
            context.close()  # flush the recording even on a failed step
        raw = Path(video.path())
    return raw, receipts


def render_job(store, job: dict, progress) -> dict:
    p = job["params"]
    spec = plan(p["url"], p.get("steps") or [], p.get("title") or "", p.get("hold_s", 2))
    tmp = Path(store.data_dir) / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="walkthrough-", dir=tmp) as folder:
        work = Path(folder)
        progress(0.05, "opening the website")
        raw, receipts = record(spec, work, progress)
        progress(0.7, "encoding the walkthrough")
        final = work / "walkthrough.mp4"
        procutil.run([ffmpeg_path(), "-y", "-i", str(raw), "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                      "-movflags", "+faststart", str(final)], timeout=180, check=True)
        info = _probe(final)
        if not info["frames"] or info["width"] != 1280 or info["height"] != 720:
            raise engine.EngineError("verify_failed", "The recorded video has no usable frames")
        progress(0.85, "building the video contact sheet")
        sheet, measured = sheet_from_video(final, fps=1, thumb_w=256, title=spec["title"] or "Website walkthrough")
        sheet_path = work / "contact-sheet.png"
        sheet.save(sheet_path)
        recipe = {"operation": "website_walkthrough", **spec, "steps": receipts, "capture": "browser-video"}
        asset = engine.import_asset(store, p["project_id"], final, "video", original_name="walkthrough.mp4",
                                    recipe=recipe, tags=["walkthrough"], source="rendered")
        contact = engine.import_asset(store, p["project_id"], sheet_path, "image", original_name="walkthrough-contact.png",
                                      recipe={"operation": "contact_sheet", "input_asset_ids": [asset["id"]], **measured},
                                      tags=["walkthrough", "contact-sheet"], source="rendered")
        return {"asset_id": asset["id"], "asset_ids": [asset["id"], contact["id"]], "contact_sheet_id": contact["id"],
                "steps": receipts, "file": info, "capture": "browser-video"}
