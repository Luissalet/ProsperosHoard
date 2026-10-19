"""Editable HTML compositions, exact-time browser frames and queued video exports."""
from __future__ import annotations

from contextlib import contextmanager
from fractions import Fraction
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

from .errors import Conflict, LumiereError, NotFound
from .ffmpeg import Cancelled

_LOCK = threading.RLock()
_ID = re.compile(r"^[0-9a-f]{32}$")
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


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
            self._receive()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _receive(self):
        deadline = time.monotonic() + self.timeout
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

    def frame(self, at: float) -> bytes:
        if self.ctx:
            self.ctx.check()
        try:
            self.process.stdin.write(json.dumps({"at": at}) + "\n")
            self.process.stdin.flush()
        except OSError:
            if self.ctx:
                self.ctx.check()
            raise LumiereError("HTML browser worker stopped.", code="html_motion_browser") from None
        import base64
        return base64.b64decode(self._receive()["png"])

    def __exit__(self, *_):
        if self.process is not None:
            if self.process.poll() is None:
                self.handle.cancel()
            self.process.wait(timeout=10)
            self.reader.join()
            for pipe in (self.process.stdin, self.process.stdout):
                if pipe is not None:
                    pipe.close()


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
             expected_revision: int | None = None, project_id: str | None = None) -> dict:
        if not html.strip() or not name.strip():
            raise LumiereError("HTML source and composition name cannot be empty.", code="html_motion")
        if width < 1 or height < 1 or not math.isfinite(fps) or fps <= 0 or not math.isfinite(duration) or duration <= 0:
            raise LumiereError("Canvas, frame rate and duration must be positive finite values.", code="html_motion")
        with _LOCK:
            current = self.get(composition) if composition else None
            if current and expected_revision != current["revision"]:
                raise Conflict("The composition changed. Reload it before saving.", code="revision_conflict")
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
            self._write(folder / "manifest.json", manifest)
            self._write(folder.parent / "current.json", manifest)
            return manifest

    def source(self, manifest: dict) -> str:
        return (self.folder(manifest["id"]) / str(manifest["revision"]) / "composition.html").read_text(encoding="utf-8")

    def restore(self, composition: str, revision: int, expected_revision: int) -> dict:
        old = self.get(composition, revision)
        return self.save(composition=composition, expected_revision=expected_revision, html=self.source(old),
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

    def frame(self, composition: str, at: float, revision: int | None = None) -> tuple[bytes, dict]:
        manifest = self.get(composition, revision)
        if not math.isfinite(at) or at < 0 or at > manifest["duration"]:
            raise LumiereError("Frame time must be within the composition, in seconds.", code="html_motion_time")
        with HtmlFrameWorker(manifest) as worker:
            data = worker.frame(at)
        return data, {"id": composition, "revision": manifest["revision"], "at_s": at,
                      "source_sha256": manifest["source_sha256"], "frame_sha256": hashlib.sha256(data).hexdigest()}

    def storyboard(self, composition: str, times: list[float] | None = None, revision: int | None = None) -> dict:
        """Capture one pinned revision/session and publish a real image review sheet."""
        from io import BytesIO
        from PIL import Image, ImageDraw, ImageFont
        manifest = self.get(composition, revision)
        moments = list(times) if times is not None else [manifest['duration']*i/3 for i in range(4)]
        if not moments or any(not math.isfinite(t) or t < 0 or t > manifest['duration'] for t in moments):
            raise LumiereError('Storyboard times must be within the composition, in seconds.', code='html_motion_time')
        try:
            captured_source = Path(manifest['source_path']).read_bytes()
        except OSError:
            raise LumiereError('HTML source could not be read.', code='html_motion_source') from None
        source_hash = hashlib.sha256(captured_source).hexdigest()
        import base64
        worker_manifest = {**manifest, 'source_sha256':source_hash, 'captured_source_b64':base64.b64encode(captured_source).decode('ascii')}
        originals, previews = [], []
        with HtmlFrameWorker(worker_manifest) as worker:
            for at in moments:
                data = worker.frame(at)
                originals.append(data)
                with Image.open(BytesIO(data)) as image:
                    preview = image.convert('RGB')
                    preview.thumbnail((512, 288), Image.Resampling.LANCZOS)
                    previews.append(preview)
        cell_width = max(192, max(image.width for image in previews))
        cell_height = max(image.height for image in previews) + 32
        columns = min(2, len(previews))
        sheet = Image.new('RGB', (cell_width*columns, cell_height*math.ceil(len(previews)/columns)), '#17191e')
        draw = ImageDraw.Draw(sheet)
        try:
            font = ImageFont.load_default(size=16)
        except TypeError:  # Pillow 10.0 keeps its original bitmap default.
            font = ImageFont.load_default()
        for i, (at, image) in enumerate(zip(moments, previews)):
            x, y = (i%columns)*cell_width, (i//columns)*cell_height
            sheet.paste(image, (x+(cell_width-image.width)//2, y))
            draw.text((x+8, y+cell_height-24), f'{at:.6g} s | r{manifest["revision"]}', fill='#ebe4ec', font=font)
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

    def queue_render(self, svc, composition: str, revision: int | None = None) -> dict:
        manifest = self.get(composition, revision)
        if manifest["width"] % 2 or manifest["height"] % 2:
            raise LumiereError("H.264 4:2:0 needs even canvas dimensions. Save an even width and height before rendering.", code="html_motion_canvas")
        job = svc.jobs.submit("html_motion", {"composition": composition, "revision": manifest["revision"]},
                              label=manifest["name"], project_id=manifest["project_id"])
        return {"job": job["id"], "state": job["state"], "composition": composition, "revision": manifest["revision"]}

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
            temporary.replace(output)
            result = {**manifest, "render_path": str(output), "render_url": f"/api/html-motion/{manifest['id']}/revisions/{manifest['revision']}/render/{ctx.id}",
                      "frames": count, "render_duration": count / float(fps), "codec": "h264", "audio": False,
                      "render_sha256": hashlib.sha256(output.read_bytes()).hexdigest()}
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


def run_render_job(svc, ctx) -> dict:
    return HtmlMotion(svc.config.data_dir).render(svc, ctx)
