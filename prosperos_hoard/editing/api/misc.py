"""/api/jobs, /api/renders, /api/frames, /api/settings, /api/presets and the folder browser used by the import dialog."""

from __future__ import annotations

import os
import string
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

from ..compression import CompressArgs
from .. import ffmpeg as ff
from ..errors import LumiereError, NotFound, Refused
from ..render import runner
from .deps import services, tool

router = APIRouter(prefix="/api")


@router.get("/jobs")
def jobs(request: Request, state: Optional[str] = None, media: Optional[str] = None, project: Optional[str] = None, limit: int = 50):
    return {"jobs": services(request).jobs.list(state=state, media_id=media, project_id=project, limit=limit)}


@router.get("/jobs/{job_id}")
def job(request: Request, job_id: str):
    return services(request).jobs.get(job_id)


@router.post("/jobs/{job_id}/cancel")
def cancel(request: Request, job_id: str):
    return services(request).jobs.cancel(job_id)


@router.post("/video/compress")
def compress_video(request: Request, body: CompressArgs):
    return tool(request, "video_compress", **body.model_dump())


@router.get("/jobs/{job_id}/file")
def compressed_file(request: Request, job_id: str):
    job = services(request).jobs.get(job_id)
    if job["kind"] != "video_compress" or job["state"] != "done":
        raise NotFound("That compression has no finished output.")
    path = Path(job["result"]["path"])
    if not path.is_file():
        raise NotFound("The compressed video is no longer on disk.")
    return FileResponse(path, filename=path.name)


@router.get("/renders")
def renders(request: Request, project: Optional[str] = None):
    return {"renders": runner.renders_list(services(request), project)}


@router.get("/renders/{render_id}/file")
def render_file(request: Request, render_id: str, download: bool = False):
    row = services(request).db.one("SELECT path FROM renders WHERE id = ?", (render_id,))
    if row is None or not Path(row["path"]).exists():
        raise NotFound("That export is not on disk any more.")
    path = Path(row["path"])
    return FileResponse(path, filename=path.name if download else None)


@router.delete("/renders/{render_id}")
def delete_render(request: Request, render_id: str, delete_file: bool = False):
    svc = services(request)
    row = svc.db.one("SELECT path FROM renders WHERE id = ?", (render_id,))
    if row is None:
        raise NotFound("No such export.")
    if delete_file:
        Path(row["path"]).unlink(missing_ok=True)
    svc.db.execute("DELETE FROM renders WHERE id = ?", (render_id,))
    return {"deleted": render_id, "file_deleted": delete_file}


@router.get("/frames/{name}")
def frame_file(request: Request, name: str):
    if "/" in name or "\\" in name or ".." in name:
        raise NotFound("No such frame.")
    path = services(request).config.renders_dir / "frames" / name
    if not path.exists():
        raise NotFound("No such frame.")
    return FileResponse(path)


@router.get("/settings")
def get_settings(request: Request):
    return services(request).get_settings()


@router.patch("/settings")
def patch_settings(request: Request, body: dict[str, Any]):
    return services(request).update_settings(body)


@router.get("/presets")
def presets(request: Request):
    return tool(request, "presets_list")


class RevealBody(BaseModel):
    path: str


@router.post("/reveal")
def reveal(request: Request, body: RevealBody):
    """Show a file in the system file manager (exports)."""
    path = Path(body.path)
    svc = services(request)
    allowed = [svc.config.renders_dir] + ([Path(svc.db.get_setting("export_folder", "") or "")] if svc.db.get_setting("export_folder", "") else [])
    known = svc.db.one("SELECT 1 FROM renders WHERE path = ?", (str(path),)) or svc.db.one("SELECT 1 FROM media WHERE path = ?", (str(path),))
    if not known and not any(_inside(path, a) for a in allowed):
        raise Refused("Only exports and imported files can be shown.")
    if not path.exists():
        raise NotFound("The file is not there.")
    if sys.platform.startswith("win"):
        subprocess.Popen(["explorer", "/select,", str(path)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path.parent)])
    return {"ok": True}


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


LUT_MAX_BYTES = 8 * 1024 * 1024


@router.get("/luts")
def lut_text(request: Request, path: str):
    """The text of a .cube LUT, so the browser preview can apply the same grade the render does. Only .cube files (the render
    refuses anything else as a LUT) and only inside LUMIERE_FILE_ROOTS when that is set, so this cannot read arbitrary files."""
    svc = services(request)
    file = Path(path).expanduser()
    if file.suffix.lower() != ".cube":
        raise Refused("Only .cube LUT files can be read here.")
    roots = list(svc.config.file_roots)
    if roots and not any(_inside(file, r) for r in roots):
        raise Refused("That file is outside the allowed folders (LUMIERE_FILE_ROOTS).")
    if not file.is_file():
        raise NotFound(f"There is no LUT at {file}.")
    if file.stat().st_size > LUT_MAX_BYTES:
        raise Refused("That LUT is too large to preview.")
    return PlainTextResponse(file.read_text(encoding="utf-8", errors="replace"))


@router.get("/fs")
def browse(request: Request, path: str = ""):
    """Folders and media files under a folder, for the import dialog. Restricted to LUMIERE_FILE_ROOTS when set."""
    svc = services(request)
    roots = list(svc.config.file_roots)
    if not path:
        if roots:
            entries = [{"name": str(r), "path": str(r), "dir": True} for r in roots if r.exists()]
        else:
            entries = [{"name": "Inicio", "path": str(Path.home()), "dir": True}]
            for sub in ("Videos", "Vídeos", "Desktop", "Escritorio", "Downloads", "Descargas", "Music", "Pictures"):
                p = Path.home() / sub
                if p.is_dir():
                    entries.append({"name": sub, "path": str(p), "dir": True})
            if sys.platform.startswith("win"):
                for letter in string.ascii_uppercase:
                    drive = Path(f"{letter}:/")
                    if os.path.exists(f"{letter}:\\"):
                        entries.append({"name": f"{letter}:", "path": str(drive), "dir": True})
        return {"path": "", "parent": None, "entries": entries}
    folder = Path(path).expanduser()
    if roots and not any(_inside(folder, r) for r in roots):
        raise Refused("That folder is outside the allowed ones (LUMIERE_FILE_ROOTS).")
    if not folder.is_dir():
        raise NotFound(f"There is no folder at {folder}.")
    entries: list[dict[str, Any]] = []
    try:
        items = sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError as error:
        raise LumiereError(f"Cannot read {folder}: {error}") from error
    for item in items:
        if item.name.startswith(".") or item.name.startswith("$"):
            continue
        try:
            if item.is_dir():
                entries.append({"name": item.name, "path": str(item), "dir": True})
            elif item.suffix.lower() in ff.MEDIA_EXTENSIONS:
                entries.append({"name": item.name, "path": str(item), "dir": False, "bytes": item.stat().st_size})
        except OSError:
            continue
        if len(entries) >= 2000:
            break
    parent = str(folder.parent) if folder.parent != folder else ""
    if roots and parent and not any(_inside(Path(parent), r) for r in roots):
        parent = ""
    return {"path": str(folder), "parent": parent, "entries": entries}
