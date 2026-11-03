"""Editable HTML compositions, exact-time browser frames and queued video exports."""
from __future__ import annotations

from contextlib import contextmanager
from fractions import Fraction
import base64
import hashlib
import importlib.util
import json
import math
import os
import queue
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

from . import ffmpeg as ff
from . import motion_sound
from .errors import Conflict, LumiereError, NotFound
from .ffmpeg import Cancelled

_LOCK = threading.RLock()
_ID = re.compile(r"^[0-9a-f]{32}$")
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


_STARTUP_TIMEOUT_S = 30.0


class HtmlFrameWorker:
    """An owned browser process: cancellation can interrupt even a stuck seek."""
    def __init__(self, manifest: dict, ctx=None):
        from .ffmpeg import RunHandle
        self.ctx = ctx
        self.handle = ctx.handle() if ctx else RunHandle()
        self.responses = queue.Queue()
        self.process = None
        try:
            self.timeout = float(os.environ.get("LUMIERE_HTML_FRAME_TIMEOUT_S", "60"))
            if not math.isfinite(self.timeout) or self.timeout <= 0:
                raise ValueError()
        except ValueError:
            raise LumiereError("LUMIERE_HTML_FRAME_TIMEOUT_S must be positive and finite.", code="html_motion_runtime") from None
        self.manifest = manifest

    def __enter__(self):
        self.process = subprocess.Popen([sys.executable, "-m", "prosperos_hoard.editing.html_motion_worker"],
                                        cwd=Path(__file__).resolve().parents[2],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        text=True, encoding="utf-8", creationflags=_NO_WINDOW)
        self.handle.attach(self.process)
        def read():
            try:
                for line in self.process.stdout:
                    self.responses.put(line)
            finally:
                self.responses.put(None)
        self.reader = threading.Thread(target=read, name="editing-html-frames", daemon=True)
        self.reader.start()
        try:
            self.process.stdin.write(json.dumps(self.manifest) + "\n")
            self.process.stdin.flush()
            self._receive(max(self.timeout, _STARTUP_TIMEOUT_S))  # browser launch and page load are not a frame
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _receive(self, timeout: float | None = None):
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            if self.ctx:
                self.ctx.check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LumiereError("HTML frame did not finish within the configured frame timeout.", code="html_motion_timeout")
            try:
                line = self.responses.get(timeout=min(.2, remaining))
            except queue.Empty:
                continue
            if line is None:
                if self.ctx:
                    self.ctx.check()
                raise LumiereError("HTML browser worker exited before returning a frame.", code="html_motion_browser")
            data = json.loads(line)
            if "error" in data:
                raise LumiereError(data["error"], code=data.get("code", "html_motion_browser"))
            return data

    def call(self, command: dict) -> dict:
        if self.ctx:
            self.ctx.check()
        try:
            self.process.stdin.write(json.dumps(command) + "\n")
            self.process.stdin.flush()
        except OSError:
            if self.ctx:
                self.ctx.check()
            raise LumiereError("HTML browser worker stopped.", code="html_motion_browser") from None
        return self._receive()

    def frame(self, at: float) -> bytes:
        import base64
        return base64.b64decode(self.call({"at": at})["png"])

    def __exit__(self, *_):
        if self.process is not None:
            if self.process.poll() is None:
                self.handle.cancel()
            self.process.wait(timeout=10)
            self.reader.join()
            for pipe in (self.process.stdin, self.process.stdout):
                if pipe is not None:
                    pipe.close()


_TEXTS_JS = """async times => {
  const out = [];
  const W = innerWidth, H = innerHeight;
  const alpha = c => { const m = /rgba?\\(([^)]+)\\)/.exec(c || ''); if (!m) return 1; const p = m[1].split(/[ ,\\/]+/).filter(Boolean); return p.length > 3 ? parseFloat(p[3]) : 1; };
  const pathOf = el => { const parts = []; for (let e = el; e && e !== document.body && e.parentElement; e = e.parentElement) { if (e.id) { parts.unshift('#' + e.id); break; } parts.unshift(e.tagName.toLowerCase() + ':' + [...e.parentElement.children].indexOf(e)); } return parts.join('>'); };
  const dom = () => {
    const list = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      const text = node.nodeValue.replace(/\\s+/g, ' ').trim();
      const el = node.parentElement;
      if (!text || !el || ['SCRIPT', 'STYLE', 'NOSCRIPT', 'TITLE'].includes(el.tagName)) continue;
      const range = document.createRange(); range.selectNodeContents(node);
      const r = range.getBoundingClientRect();
      if (r.width < 1 || r.height < 1) continue;
      let opacity = alpha(getComputedStyle(el).color), hidden = false, x0 = r.left, y0 = r.top, x1 = r.right, y1 = r.bottom, clip = 1;
      for (let e = el; e; e = e.parentElement) {
        const cs = getComputedStyle(e);
        if (cs.display === 'none' || cs.visibility === 'hidden') hidden = true;
        opacity *= parseFloat(cs.opacity);
        if (e !== document.documentElement && (cs.overflow !== 'visible' || cs.overflowX !== 'visible' || cs.overflowY !== 'visible' || cs.clipPath !== 'none')) {
          const b = e.getBoundingClientRect();
          const w = Math.max(0, Math.min(x1, b.right) - Math.max(x0, b.left)), h = Math.max(0, Math.min(y1, b.bottom) - Math.max(y0, b.top));
          clip = Math.min(clip, (w * h) / Math.max(1e-9, (x1 - x0) * (y1 - y0)));
        }
      }
      const index = [...el.childNodes].indexOf(node);
      list.push({id: pathOf(el) + (index > 0 ? '~' + index : ''), text, x0, y0, x1, y1, opacity: hidden ? 0 : opacity, frac: clip});
    }
    return list;
  };
  const custom = typeof window.TEXTS === 'function';
  for (const t of times) {
    await window.seek(t); await document.fonts.ready;
    out.push(custom ? (window.TEXTS(t) || []).map(o => ({id: String(o.id), text: String(o.text), x0: +o.x0, y0: +o.y0, x1: +o.x1, y1: +o.y1,
      opacity: o.opacity === undefined ? 1 : +o.opacity, frac: o.frac === undefined ? 1 : +o.frac})) : dom());
  }
  return {source: custom ? 'TEXTS' : 'dom', frames: out, width: W, height: H};
}"""


def browser_executable() -> str | None:
    configured = os.environ.get("LUMIERE_BROWSER_EXECUTABLE", "").strip()
    if configured:
        return configured
    for name in ("chromium", "chromium-browser", "google-chrome", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    if os.name == "nt":
        for key in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
            for relative in ("Microsoft/Edge/Application/msedge.exe", "Google/Chrome/Application/chrome.exe"):
                path = Path(os.environ.get(key, "")) / relative
                if path.is_file():
                    return str(path)
    return None


class HtmlMotion:
    def __init__(self, data_dir: Path):
        self.root = Path(data_dir).resolve() / "creative" / "html"

    def status(self) -> dict:
        installed = importlib.util.find_spec("playwright") is not None
        return {"playwright": installed, "browser_executable": browser_executable(),
                "hint": "Install lumiere-hoard[motion] and run python -m playwright install chromium if no system browser is available."}

    def folder(self, composition: str) -> Path:
        if not _ID.fullmatch(composition):
            raise LumiereError("Invalid HTML composition id.", code="html_motion")
        folder = (self.root / composition).resolve()
        if not folder.is_relative_to(self.root):
            raise LumiereError("Invalid HTML composition folder.", code="html_motion")
        return folder

    def get(self, composition: str, revision: int | None = None) -> dict:
        folder = self.folder(composition)
        path = folder / "current.json" if revision is None else folder / str(revision) / "manifest.json"
        if not path.is_file():
            raise NotFound("HTML composition or revision not found.")
        return json.loads(path.read_text(encoding="utf-8"))

    def list(self) -> dict:
        items = []
        for path in self.root.glob("*/current.json"):
            items.append(json.loads(path.read_text(encoding="utf-8")))
        return {"compositions": sorted(items, key=lambda x: x["updated_ts"], reverse=True), "runtime": self.status()}

    @staticmethod
    def _write(path: Path, value: dict) -> None:
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)

    def save(self, *, html: str, name: str = "HTML motion", width: int = 1280, height: int = 720,
             fps: float = 30, duration: float = 3, composition: str | None = None,
             expected_revision: int | None = None, project_id: str | None = None, sound: dict | None = None) -> dict:
        if not html.strip() or not name.strip():
            raise LumiereError("HTML source and composition name cannot be empty.", code="html_motion")
        if width < 1 or height < 1 or not math.isfinite(fps) or fps <= 0 or not math.isfinite(duration) or duration <= 0:
            raise LumiereError("Canvas, frame rate and duration must be positive finite values.", code="html_motion")
        with _LOCK:
            current = self.get(composition) if composition else None
            if current and expected_revision != current["revision"]:
                raise Conflict("The composition changed. Reload it before saving.", code="revision_conflict")
            declared = motion_sound.normalise(sound, duration) if sound is not None else None
            if sound is None:
                found = motion_sound.declared_in_html(html)
                declared = motion_sound.normalise(found, duration) if found is not None else (current or {}).get("sound")
            composition = composition or uuid.uuid4().hex
            revision = current["revision"] + 1 if current else 1
            folder = self.folder(composition) / str(revision)
            folder.mkdir(parents=True)
            source = folder / "composition.html"
            source.write_bytes(html.encode("utf-8"))
            base = f"/api/html-motion/{composition}/revisions/{revision}"
            manifest = {"id": composition, "revision": revision, "name": name.strip(), "width": width, "height": height,
                        "fps": fps, "duration": duration, "project_id": project_id, "updated_ts": time.time(),
                        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "source_path": str(source),
                        "source_url": base + "/source", "frame_url": base + "/frame", "render_url": None}
            if declared:
                manifest["sound"] = declared
            self._write(folder / "manifest.json", manifest)
            self._write(folder.parent / "current.json", manifest)
            return manifest

    def source(self, manifest: dict) -> str:
        return (self.folder(manifest["id"]) / str(manifest["revision"]) / "composition.html").read_text(encoding="utf-8")

    def delete(self, svc, composition: str) -> dict:
        """Delete a composition with every revision, render, storyboard, sound preview, treatment, cues and facts it keeps
        in its folder. Refuses while an export of it is queued or running, and while a library media still reads one of
        its rendered videos (remove that media first: the library imports a render where it is, without a copy)."""
        folder = self.folder(composition)
        manifest = self.get(composition)
        if folder.is_symlink() or not folder.is_relative_to(self.root):
            raise LumiereError("Invalid HTML composition folder.", code="html_motion")
        busy = [job for job in svc.jobs.list(state="active", limit=500)
                if job.get("kind") == "html_motion" and (job.get("params") or {}).get("composition") == composition]
        if busy:
            raise Conflict(f"{manifest['name']} is being exported (job {busy[0]['id']}): cancel the export or wait for it before deleting it.",
                           code="html_motion_busy")
        prefix = str(folder)
        used = [row for row in svc.db.query("SELECT id, name, path FROM media WHERE path LIKE ?", (prefix + "%",))
                if Path(row["path"]).is_relative_to(folder)]
        if used:
            names = ", ".join(f"{row['name']} ({row['id']})" for row in used[:6])
            raise Conflict(f"The library still uses a video of {manifest['name']}: {names}. Remove it from the library first (media_delete).",
                           code="html_motion_in_use")
        with _LOCK:
            revisions = sorted(int(p.name) for p in folder.iterdir() if p.is_dir() and p.name.isdigit())
            files = [p for p in folder.rglob("*") if p.is_file()]
            renders = sum(1 for p in files if p.name.startswith("render-") and p.suffix == ".mp4")
            shutil.rmtree(folder)
        return {"deleted": composition, "name": manifest["name"], "revisions": revisions, "renders": renders,
                "files_removed": len(files)}

    def restore(self, composition: str, revision: int, expected_revision: int) -> dict:
        old = self.get(composition, revision)
        return self.save(composition=composition, expected_revision=expected_revision, html=self.source(old),
                         sound=old.get("sound") or {},
                         **{k: old[k] for k in ("name", "width", "height", "fps", "duration", "project_id")})

    @contextmanager
    def page(self, manifest: dict):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise LumiereError(self.status()["hint"], code="html_motion_runtime") from None
        with sync_playwright() as runtime:
            browser = None
            try:
                browser = runtime.chromium.launch(headless=True, executable_path=browser_executable(), args=["--disable-gpu"])
                page = browser.new_page(viewport={"width": manifest["width"], "height": manifest["height"]},
                                        device_scale_factor=1, locale="es-ES", timezone_id="Europe/Madrid")
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                if 'captured_source_b64' in manifest:
                    import base64
                    captured = base64.b64decode(manifest['captured_source_b64'], validate=True)
                    if hashlib.sha256(captured).hexdigest() != manifest['source_sha256']:
                        raise LumiereError('Captured HTML does not match its source hash.', code='html_motion_integrity')
                    # Serve the verified in-memory document at its original URL:
                    # relative resources and location semantics remain unchanged.
                    page.route(Path(manifest['source_path']).as_uri(), lambda route: route.fulfill(body=captured, content_type='text/html; charset=utf-8'))
                page.goto(Path(manifest["source_path"]).as_uri(), wait_until="load")
                page.evaluate("async () => { await document.fonts.ready; await Promise.all([...document.images].map(i => i.decode())); }")
                if not page.evaluate("typeof window.seek === 'function'"):
                    raise LumiereError("Define window.seek(t) in the HTML, with t in seconds.", code="html_motion_seek")
                yield page, errors
            except LumiereError:
                raise
            except Exception as exc:
                raise LumiereError(f"HTML frame failed: {exc}", code="html_motion_browser") from None
            finally:
                if browser is not None:
                    browser.close()

    @staticmethod
    def capture(page, errors: list, at: float) -> bytes:
        page.evaluate("async t => { await window.seek(t); await document.fonts.ready; }", at)
        if errors:
            raise LumiereError("HTML script failed: " + "; ".join(errors), code="html_motion_script")
        return page.screenshot(type="png", animations="disabled")

    @staticmethod
    def texts(page, errors: list, times: list[float]) -> dict:
        """The texts on screen at each time: window.TEXTS(t) when the page defines it, else the visible text of the DOM."""
        result = page.evaluate(_TEXTS_JS, times)
        if errors:
            raise LumiereError("HTML script failed: " + "; ".join(errors), code="html_motion_script")
        return result

    @staticmethod
    def box(page, errors: list, at: float, selector: str) -> dict | None:
        page.evaluate("async t => { await window.seek(t); await document.fonts.ready; }", at)
        if errors:
            raise LumiereError("HTML script failed: " + "; ".join(errors), code="html_motion_script")
        return page.evaluate("""s => { const e = document.querySelector(s); if (!e) return null;
            const r = e.getBoundingClientRect(); return {x0: r.left, y0: r.top, x1: r.right, y1: r.bottom}; }""", selector)

    @staticmethod
    def pinned(manifest: dict) -> dict:
        """The manifest with its source captured in memory, so every frame of one review comes from the same bytes."""
        import base64
        try:
            captured = Path(manifest['source_path']).read_bytes()
        except OSError:
            raise LumiereError('HTML source could not be read.', code='html_motion_source') from None
        return {**manifest, 'source_sha256': hashlib.sha256(captured).hexdigest(),
                'captured_source_b64': base64.b64encode(captured).decode('ascii')}

    def frame(self, composition: str, at: float, revision: int | None = None) -> tuple[bytes, dict]:
        manifest = self.get(composition, revision)
        if not math.isfinite(at) or at < 0 or at > manifest["duration"]:
            raise LumiereError("Frame time must be within the composition, in seconds.", code="html_motion_time")
        with HtmlFrameWorker(manifest) as worker:
            data = worker.frame(at)
        return data, {"id": composition, "revision": manifest["revision"], "at_s": at,
                      "source_sha256": manifest["source_sha256"], "frame_sha256": hashlib.sha256(data).hexdigest()}

    @staticmethod
    def _sheet(previews: list, labels: list[str], columns: int, minimum: int = 192):
        """A contact sheet: each preview with its label underneath."""
        from PIL import Image, ImageDraw, ImageFont
        cell_width = max(minimum, max(image.width for image in previews))
        cell_height = max(image.height for image in previews) + 32
        sheet = Image.new('RGB', (cell_width*columns, cell_height*math.ceil(len(previews)/columns)), '#17191e')
        draw = ImageDraw.Draw(sheet)
        try:
            font = ImageFont.load_default(size=16)
        except TypeError:  # Pillow 10.0 keeps its original bitmap default.
            font = ImageFont.load_default()
        for i, (image, label) in enumerate(zip(previews, labels)):
            x, y = (i%columns)*cell_width, (i//columns)*cell_height
            sheet.paste(image, (x+(cell_width-image.width)//2, y))
            draw.text((x+8, y+cell_height-24), label, fill='#ebe4ec', font=font)
        return sheet

    def storyboard(self, composition: str, times: list[float] | None = None, revision: int | None = None, *, mode: str = 'sheet',
                   span: tuple[float, float] | None = None, x: float | None = None, y: float | None = None,
                   selector: str | None = None, size: tuple[int, int] | None = None, scale: float = 2.0) -> dict:
        """Capture one pinned revision/session and publish a real image review sheet.

        ``sheet``: the given moments, each shrunk. ``strip``: every frame of a span at the composition's frame rate
        (at most 48, evenly thinned), small, to see the motion between two moments. ``crop-at``: the given moments
        cropped around a point (x, y) or a CSS selector and enlarged, to see detail."""
        from io import BytesIO
        from PIL import Image
        if mode not in ('sheet', 'strip', 'crop-at'):
            raise LumiereError('Review mode must be sheet, strip or crop-at.', code='html_motion_mode')
        manifest = self.get(composition, revision)
        duration, fps = manifest['duration'], manifest['fps']
        step_frames = 1
        if mode == 'strip':
            start, end = span if span else (0.0, duration)
            if not (math.isfinite(start) and math.isfinite(end)) or start < 0 or end > duration or end < start:
                raise LumiereError('Strip span must be inside the composition, in seconds.', code='html_motion_time')
            total = int((end - start) * fps + 1e-9) + 1
            step_frames = max(1, math.ceil(total / 48))
            moments = [round(start + k / fps, 6) for k in range(0, total, step_frames)]
        else:
            moments = list(times) if times is not None else [duration*i/3 for i in range(4)]
        if not moments or any(not math.isfinite(t) or t < 0 or t > duration for t in moments):
            raise LumiereError('Storyboard times must be within the composition, in seconds.', code='html_motion_time')
        if mode == 'crop-at' and selector is None and (x is None or y is None):
            raise LumiereError('crop-at needs a point (x, y) or a CSS selector.', code='html_motion_crop')
        worker_manifest = self.pinned(manifest)
        captured_source = base64.b64decode(worker_manifest['captured_source_b64'])
        source_hash = worker_manifest['source_sha256']
        originals, previews, labels, crops = [], [], [], []
        with HtmlFrameWorker(worker_manifest) as worker:
            for at in moments:
                data = worker.frame(at)
                originals.append(data)
                with Image.open(BytesIO(data)) as image:
                    full = image.convert('RGB')
                if mode == 'sheet':
                    preview = full.copy(); preview.thumbnail((512, 288), Image.Resampling.LANCZOS)
                    labels.append(f'{at:.6g} s | r{manifest["revision"]}')
                elif mode == 'strip':
                    preview = full.copy(); preview.thumbnail((240, 135), Image.Resampling.BOX)
                    labels.append(f'{at:.3f}')
                else:
                    if selector is not None:
                        box = worker.call({'op': 'box', 'at': at, 'selector': selector})['box']
                        if box is None:
                            raise LumiereError(f'No element matches {selector!r} at {at:g} s.', code='html_motion_crop')
                        pad_x, pad_y = (box['x1']-box['x0'])*.08+4, (box['y1']-box['y0'])*.08+4
                        rect = [box['x0']-pad_x, box['y0']-pad_y, box['x1']+pad_x, box['y1']+pad_y]
                    else:
                        w, h = size or (max(64, manifest['width']//4), max(36, manifest['height']//4))
                        rect = [x-w/2, y-h/2, x+w/2, y+h/2]
                    left = max(0, min(manifest['width']-1, int(rect[0]))); top = max(0, min(manifest['height']-1, int(rect[1])))
                    right = max(left+1, min(manifest['width'], int(math.ceil(rect[2])))); bottom = max(top+1, min(manifest['height'], int(math.ceil(rect[3]))))
                    preview = full.crop((left, top, right, bottom))
                    wide = min(640, max(64, int(preview.width*scale)))
                    preview = preview.resize((wide, max(1, round(preview.height*wide/preview.width))), Image.Resampling.LANCZOS)
                    crops.append({'at_s': at, 'x0': left, 'y0': top, 'x1': right, 'y1': bottom})
                    labels.append(f'{at:.6g} s | {left},{top}-{right},{bottom}')
                previews.append(preview)
        columns = 8 if mode == 'strip' and len(previews) > 8 else min(2, len(previews))
        if mode == 'strip' and len(previews) <= 8:
            columns = len(previews)
        sheet = self._sheet(previews, labels, columns, 120 if mode == 'strip' else 192)
        output = BytesIO();sheet.save(output, format='PNG');png=output.getvalue()
        identity = uuid.uuid4().hex
        destination = (self.folder(composition)/str(manifest['revision'])/'storyboards'/identity).resolve()
        if not destination.is_relative_to(self.root):
            raise LumiereError('Invalid storyboard folder.', code='html_motion')
        destination.mkdir(parents=True, exist_ok=False)
        base = f'/api/html-motion/{composition}/revisions/{manifest["revision"]}/storyboards/{identity}'
        result = {'id':identity, 'composition':composition, 'revision':manifest['revision'], 'times':moments,
                  'source_sha256':source_hash, 'saved_source_sha256':manifest['source_sha256'],
                  'source_matches_revision':source_hash==manifest['source_sha256'], 'source_url':base+'/source',
                  'image_sha256':hashlib.sha256(png).hexdigest(),
                  'width':sheet.width, 'height':sheet.height, 'url':base+'/image', 'frames':[]}
        if mode != 'sheet':
            result['mode'] = mode
        if mode == 'strip':
            result['every_frames'] = step_frames
            result['fps'] = fps
        if crops:
            result['crops'] = crops
        # The receipt is the publication marker; failures remove only these new derived files.
        try:
            (destination/'source.html').write_bytes(captured_source)
            for i, (at, data) in enumerate(zip(moments, originals)):
                (destination/f'frame-{i}.png').write_bytes(data)
                result['frames'].append({'at_s':at, 'sha256':hashlib.sha256(data).hexdigest(), 'url':base+f'/frames/{i}'})
            (destination/'sheet.png').write_bytes(png)
            self._write(destination/'manifest.json', result)
        except BaseException:
            for file in destination.iterdir():file.unlink()
            destination.rmdir()
            raise
        return result

    def storyboard_image(self, composition: str, revision: int, storyboard: str, frame: int | None = None) -> bytes:
        manifest = self.get(composition, revision)
        if not _ID.fullmatch(storyboard):
            raise LumiereError('Invalid storyboard id.', code='html_motion')
        folder = (self.folder(composition)/str(revision)/'storyboards'/storyboard).resolve()
        if not folder.is_relative_to(self.root):
            raise LumiereError('Invalid storyboard folder.', code='html_motion')
        receipt = folder/'manifest.json'
        if not receipt.is_file():raise NotFound('Storyboard not found.')
        result = json.loads(receipt.read_text(encoding='utf-8'))
        if result['composition'] != composition or result['revision'] != revision or result.get('saved_source_sha256',result['source_sha256']) != manifest['source_sha256']:
            raise LumiereError('Storyboard source does not match its revision.', code='html_motion_integrity')
        if frame is not None:
            if frame < 0 or frame >= len(result['frames']):raise NotFound('Storyboard frame not found.')
            path, expected = folder/f'frame-{frame}.png', result['frames'][frame]['sha256']
        else:
            path, expected = folder/'sheet.png', result['image_sha256']
        if not path.is_file():raise NotFound('Storyboard image not found.')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest()!=expected:
            raise LumiereError('Storyboard image no longer matches its recorded hash.', code='html_motion_integrity')
        return data

    def storyboard_source(self, composition: str, revision: int, storyboard: str) -> bytes:
        self.storyboard_image(composition, revision, storyboard)
        folder = (self.folder(composition)/str(revision)/'storyboards'/storyboard).resolve()
        result = json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
        source = folder/'source.html'
        if not source.is_file():raise NotFound('Captured HTML is not available for this older sheet.')
        data=source.read_bytes()
        if hashlib.sha256(data).hexdigest()!=result['source_sha256']:
            raise LumiereError('Captured HTML no longer matches its recorded hash.', code='html_motion_integrity')
        return data

    def queue_render(self, svc, composition: str, revision: int | None = None, audio: bool = True) -> dict:
        manifest = self.get(composition, revision)
        if manifest["width"] % 2 or manifest["height"] % 2:
            raise LumiereError("H.264 4:2:0 needs even canvas dimensions. Save an even width and height before rendering.", code="html_motion_canvas")
        params = {"composition": composition, "revision": manifest["revision"]}
        if not audio:
            params["audio"] = False  # absent = with the declared sound; the key only appears when it was switched off
        job = svc.jobs.submit("html_motion", params, label=manifest["name"], project_id=manifest["project_id"])
        return {"job": job["id"], "state": job["state"], "composition": composition, "revision": manifest["revision"],
                "audio": bool(audio and (manifest.get("sound") or {}).get("audio", True) and manifest.get("sound"))}

    def sound_preview(self, svc, composition: str, revision: int | None = None, ctx=None) -> dict:
        """Mix and master the declared sound of one revision into a WAV next to it, without rendering any picture."""
        manifest = self.get(composition, revision)
        sound = manifest.get("sound")
        if not sound:
            raise LumiereError("This composition declares no sound. Add events with motion_sound_events.", code="no_sound")
        folder = self.folder(manifest["id"]) / str(manifest["revision"]) / "sound"
        folder.mkdir(parents=True, exist_ok=True)
        fps = Fraction(str(manifest["fps"]))
        duration = float(Fraction(math.ceil(Fraction(str(manifest["duration"])) * fps), 1) / fps)
        digest = hashlib.sha256(json.dumps([sound, duration], sort_keys=True).encode("utf-8")).hexdigest()[:16]
        destination = folder / f"preview-{digest}.wav"
        report = motion_sound.build_soundtrack(svc, sound, duration, destination, ctx)
        self._write(folder / f"preview-{digest}.json", report)
        base = f"/api/html-motion/{manifest['id']}/revisions/{manifest['revision']}/sound/{digest}"
        return {"composition": manifest["id"], "revision": manifest["revision"], "id": digest, "url": base + "/audio",
                **{k: v for k, v in report.items() if k != "path"}}

    def sound_file(self, composition: str, revision: int, digest: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{16}", digest):
            raise LumiereError("Invalid sound id.", code="html_motion")
        path = self.folder(composition) / str(revision) / "sound" / f"preview-{digest}.wav"
        if not path.is_file():
            raise NotFound("Sound preview not found.")
        return path

    def render(self, svc, ctx) -> dict:
        manifest = self.get(ctx.params["composition"], ctx.params["revision"])
        folder = self.folder(manifest["id"]) / str(manifest["revision"])
        output = folder / f"render-{ctx.id}.mp4"
        temporary = folder / f"render-{ctx.id}.partial.mp4"
        fps = Fraction(str(manifest["fps"]))
        count = math.ceil(Fraction(str(manifest["duration"])) * fps)
        stderr_path = folder / f"render-{ctx.id}.log"
        handle = ctx.handle()
        process = None
        try:
            with HtmlFrameWorker(manifest, ctx) as worker, stderr_path.open("wb") as stderr:
                ctx.check()
                process = subprocess.Popen([svc.tools().ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "image2pipe",
                                            "-framerate", str(fps), "-i", "pipe:0", "-an", "-c:v", "libx264", "-preset", "medium",
                                            "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temporary)],
                                           stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=stderr, creationflags=_NO_WINDOW)
                handle.attach(process)
                for frame in range(count):
                    ctx.check()
                    process.stdin.write(worker.frame(float(Fraction(frame, 1) / fps)))
                    ctx.progress((frame + 1) / count * .95, f"{frame + 1}/{count} frames")
                process.stdin.close()
                if process.wait(timeout=120) != 0:
                    raise LumiereError("HTML video encoding failed: " + stderr_path.read_text(errors="replace"), code="html_motion_encode")
            ctx.check()
            probe = subprocess.run([svc.tools().ffprobe, "-v", "error", "-count_frames", "-show_streams", "-show_format", "-of", "json", str(temporary)],
                                   capture_output=True, text=True, check=True, timeout=60, creationflags=_NO_WINDOW)
            metadata = json.loads(probe.stdout)
            video = next(s for s in metadata["streams"] if s["codec_type"] == "video")
            valid = (video["codec_name"] == "h264" and video["width"] == manifest["width"] and video["height"] == manifest["height"]
                     and int(video["nb_read_frames"]) == count and Fraction(video["avg_frame_rate"]) == fps
                     and abs(float(metadata["format"]["duration"]) - float(Fraction(count, 1) / fps)) < .01)
            if not valid:
                raise LumiereError("Encoded video does not match the composition canvas, frame rate or frame count.", code="html_motion_verify")
            sound = manifest.get("sound")
            audio_report = None
            if sound and sound.get("audio", True) and ctx.params.get("audio", True):
                audio_report = self._add_sound(svc, ctx, sound, temporary, folder, count / float(fps))
            temporary.replace(output)
            result = {**manifest, "render_path": str(output), "render_url": f"/api/html-motion/{manifest['id']}/revisions/{manifest['revision']}/render/{ctx.id}",
                      "frames": count, "render_duration": count / float(fps), "codec": "h264", "audio": audio_report is not None,
                      "render_sha256": hashlib.sha256(output.read_bytes()).hexdigest()}
            if audio_report is not None:
                result["sound_report"] = audio_report
            with _LOCK:
                self._write(folder / "manifest.json", result)
                if self.get(manifest["id"])["revision"] == manifest["revision"]:
                    self._write(folder.parent / "current.json", result)
            ctx.progress(1, "HTML video ready", force=True)
            return result
        except Cancelled:
            raise
        except LumiereError:
            raise
        except Exception as exc:
            ctx.check()
            raise LumiereError(f"HTML video export failed: {exc}", code="html_motion_encode") from None
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            if temporary.exists():
                temporary.unlink()


    def _add_sound(self, svc, ctx, sound: dict, video: Path, folder: Path, duration: float) -> dict:
        """Build the declared soundtrack and mux it into the encoded video (the picture is copied, not encoded again).
        AAC can overshoot the true peak a little: the delivered file is measured and the mix redone with more headroom if needed."""
        wav = folder / f"render-{ctx.id}.wav"
        muxed = folder / f"render-{ctx.id}.sound.mp4"
        kept = folder / f"render-{ctx.id}.best.mp4"
        tools = svc.tools()
        # ffmpeg prints the true peak with one decimal: asking for half of that under the ceiling keeps the claim true
        ceiling = float(sound.get("true_peak_db", motion_sound.TRUE_PEAK_DB)) - .05
        tried: list[tuple[float, float]] = []  # (headroom, delivered true peak) of every attempt
        best, report = None, None
        try:
            headroom = 0.0
            for _attempt in range(5):
                ctx.progress(.96, "sound")
                report = motion_sound.build_soundtrack(svc, {**sound, "_headroom": headroom}, duration, wav, ctx)
                ctx.check()
                ff.run(tools, ["-i", str(video), "-i", str(wav), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                               "-ar", "48000", "-shortest", "-movflags", "+faststart", str(muxed)], handle=ctx.handle())
                measured = ff.loudness(tools, muxed)
                peak = measured.get("true_peak_dbtp")
                report["delivered"] = {"lufs": measured.get("integrated_lufs"), "true_peak_db": peak}
                tried.append((headroom, peak if peak is not None else ceiling))
                if peak is None or peak <= ceiling:
                    if best is None or (report["delivered"]["lufs"] or -99) > (best["delivered"]["lufs"] or -99):
                        muxed.replace(kept)
                        best = report
                    if peak is None or peak >= ceiling - .4 or not any(p > ceiling for _h, p in tried):
                        break  # at the ceiling, or never over it: nothing to win back
                # AAC overshoots a little; walk the headroom towards the ceiling instead of overcorrecting
                over = [(h, p) for h, p in tried if p > ceiling]
                under = [(h, p) for h, p in tried if p <= ceiling]
                if over and under:
                    (h0, p0), (h1, p1) = max(over), min(under, key=lambda item: -item[1])
                    headroom = h0 + (p0 - (ceiling - .05)) / max(p0 - p1, .05) * (h1 - h0)
                else:
                    last_h, last_p = tried[-1]
                    headroom = last_h + (last_p - ceiling) + .1 if last_p > ceiling else max(0.0, last_h - .3)
                if best is not None and under and len(tried) >= 4:
                    break
            if best is None:
                muxed.replace(kept)       # nothing met the ceiling after every attempt: deliver the last one, the report says what was measured
                best = report
            report = best
            kept.replace(muxed)
            probe = subprocess.run([tools.ffprobe, "-v", "error", "-show_streams", "-of", "json", str(muxed)],
                                   capture_output=True, text=True, check=True, timeout=60, creationflags=_NO_WINDOW)
            streams = json.loads(probe.stdout)["streams"]
            audio = next((s for s in streams if s["codec_type"] == "audio"), None)
            picture = next(s for s in streams if s["codec_type"] == "video")
            if audio is None or audio["codec_name"] != "aac" or picture["codec_name"] != "h264":
                raise LumiereError("The exported video does not carry the expected H.264 picture and AAC sound.", code="html_motion_verify")
            report["audio_codec"] = audio["codec_name"]
            report["audio_duration_s"] = float(audio.get("duration") or 0)
            muxed.replace(video)
            report.pop("path", None)
            return report
        finally:
            for leftover in (wav, muxed, kept):
                if leftover.exists():
                    leftover.unlink()


def run_render_job(svc, ctx) -> dict:
    return HtmlMotion(svc.config.data_dir).render(svc, ctx)
