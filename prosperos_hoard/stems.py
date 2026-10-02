"""Stems: a song split into vocals, drums, bass and the rest (Demucs
htdemucs), as derived audio assets of the song.

The split runs in ComfyUI's own Python (it already has a CUDA torch); the
few pure-Python packages Demucs needs on top are installed once into the
data folder (`tools/demucs-lib`), never into ComfyUI's environment. Audio
goes in and out as raw float32 through ffmpeg, so nothing depends on
torchaudio's file I/O. The vocals feed lip sync (a clean voice for the
audio encoder) and the drums the beat effects ("kick")."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

from . import procutil
from .backend import ffmpeg_path
from .ids import new_id
from .store import NotFound, Store
from .util import now_iso

STEMS = ("vocals", "drums", "bass", "other")
MODEL = "htdemucs"
SR = 44100
LIB_DIR = "tools/demucs-lib"
PACKAGES = ["demucs==4.0.1", "julius==0.2.7", "openunmix==1.3.0", "omegaconf==2.3.0", "antlr4-python3-runtime==4.9.3",
            "einops"]
MARKER = "installed-4.0.1.json"


class StemsError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


# The script ComfyUI's Python runs: reads raw float32 stereo, writes one raw
# float32 file per stem. `dora` (a training tool Demucs imports two helpers
# from) is stubbed instead of installed.
RUNNER = r'''
import json, sys, types
lib, src, n, out_dir, device, model_name = sys.argv[1:7]
sys.path.insert(0, lib)
dora = types.ModuleType("dora"); log = types.ModuleType("dora.log")
log.fatal = lambda *a, **k: (_ for _ in ()).throw(SystemExit(" ".join(map(str, a))))
log.bold = lambda x: x
dora.log = log
sys.modules.setdefault("dora", dora); sys.modules.setdefault("dora.log", log)
import os
unmix = types.ModuleType("openunmix")  # only its filtering module: the package imports torchaudio on load
unmix.__path__ = [os.path.join(lib, "openunmix")]
sys.modules.setdefault("openunmix", unmix)
import numpy as np, torch
from demucs.pretrained import get_model
from demucs.apply import apply_model
wav = np.fromfile(src, dtype=np.float32).reshape(-1, 2).T.copy()
model = get_model(model_name)
model.eval()
dev = device if (device != "cpu" and torch.cuda.is_available()) else "cpu"
x = torch.from_numpy(wav)
ref = x.mean(0)
x = (x - ref.mean()) / (ref.std() + 1e-8)
with torch.no_grad():
    out = apply_model(model, x[None], device=dev, split=True, overlap=0.25, progress=False, num_workers=0)[0]
out = out * (ref.std() + 1e-8) + ref.mean()
names = list(model.sources)
for i, name in enumerate(names):
    out[i].T.contiguous().numpy().astype(np.float32).tofile(f"{out_dir}/{name}.f32")
print(json.dumps({"stems": names, "device": dev}))
'''


def lib_dir(data_dir: Path) -> Path:
    return Path(data_dir) / LIB_DIR


def installed(data_dir: Path) -> bool:
    return (lib_dir(data_dir) / MARKER).is_file()


def install(data_dir: Path, python: Path, log: Callable[[str], None] = lambda _m: None) -> None:
    """The packages Demucs needs beyond torch, into the data folder (pip
    --target, no dependencies: torch, numpy, tqdm and yaml come from
    ComfyUI's environment)."""
    target = lib_dir(data_dir)
    target.mkdir(parents=True, exist_ok=True)
    log("installing Demucs (once)")
    proc = procutil.run([str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-deps", "--upgrade",
                         "--target", str(target), *PACKAGES], text=True, timeout=900)
    if proc.returncode != 0:
        raise StemsError("install_failed", f"could not install Demucs: {(proc.stderr or proc.stdout or '')[-500:]}")
    (target / MARKER).write_text(json.dumps({"packages": PACKAGES, "at": now_iso()}), encoding="utf-8")


def pick_device(min_free_mb: int = 3000) -> str:
    """The card with the most free memory when it has room for the model,
    else the CPU (slower, but never fights a model already on a card)."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return "cpu"
    try:
        proc = procutil.run([exe, "--query-gpu=index,memory.free", "--format=csv,noheader,nounits"], text=True, timeout=15)
        rows = [tuple(int(x) for x in ln.split(",")) for ln in proc.stdout.strip().splitlines() if ln.strip()]
    except (OSError, ValueError, subprocess.SubprocessError):
        return "cpu"
    if not rows:
        return "cpu"
    idx, free = max(rows, key=lambda r: r[1])
    return f"cuda:{idx}" if free >= min_free_mb else "cpu"


def existing(store: Store, song_id: str) -> dict[str, str]:
    """The stems already split from a song: {stem: asset_id}."""
    out: dict[str, str] = {}
    try:
        song = store.get_asset(song_id)
    except NotFound:
        return out
    for a in store.list_assets(song["project_id"], kind="audio", query=song_id, tag="stem", limit=60)["items"]:
        r = a.get("recipe") or {}
        if r.get("operation") == "stems" and r.get("derived_from") == song_id and r.get("stem") in STEMS + ("instrumental",):
            out.setdefault(r["stem"], a["id"])
    return out


def mix_instrumental(store: Store, song: dict[str, Any], stems: dict[str, str]) -> str:
    """The song without its voice (drums + bass + other), for karaoke or a
    version to sing over; a derived audio asset like the stems."""
    exe = ffmpeg_path()
    if not exe:
        raise StemsError("no_ffmpeg", "ffmpeg is needed to mix the instrumental")
    parts = [store.data_dir / store.get_asset(stems[k])["file_path"] for k in ("drums", "bass", "other")]
    aid = new_id("a")
    out = store.path_for_asset_file(aid, ".flac")
    cmd = [exe, "-nostdin", "-y", "-loglevel", "error"]
    for p in parts:
        cmd += ["-i", str(p)]
    cmd += ["-filter_complex", "amix=inputs=3:normalize=0:duration=longest", "-c:a", "flac", str(out)]
    proc = procutil.run(cmd, timeout=600)
    if proc.returncode != 0 or not out.is_file():
        raise StemsError("encode_failed", "could not mix the instrumental")
    return store.create_asset(
        project_id=song["project_id"], kind="audio", file_path=out.relative_to(store.data_dir).as_posix(), mime="audio/flac",
        duration_s=song.get("duration_s"), source="derived", asset_id=aid,
        name=f"{Path(song.get('name') or song['id']).stem} - instrumental"[:100], tags=["stem", "instrumental"],
        recipe={"operation": "stems", "stem": "instrumental", "derived_from": song["id"],
                "input_asset_ids": [stems[k] for k in ("drums", "bass", "other")], "backend": "local",
                "created_at": now_iso()})["id"]


def separate(store: Store, song_id: str, python: Optional[Path], progress: Callable[[float, str], None],
             device: Optional[str] = None, force: bool = False) -> dict[str, Any]:
    """Split a song into its stems (see the module doc). Stems already made
    are returned as they are unless `force`."""
    song = store.get_asset(song_id)
    if song["kind"] not in ("audio", "video"):
        raise StemsError("not_audio", f"asset {song_id} is {song['kind']}: stems come out of a song or a video's sound")
    have = existing(store, song_id)
    if not force and all(s in have for s in STEMS):
        if "instrumental" not in have:
            have["instrumental"] = mix_instrumental(store, song, have)
        return {"song_asset_id": song_id, "stems": have, "reused": True}
    exe = ffmpeg_path()
    if not exe:
        raise StemsError("no_ffmpeg", "ffmpeg is needed to split a song")
    if not python or not Path(python).is_file():
        raise StemsError("no_python", "stems run in ComfyUI's Python environment, which was not found (Backends)")
    if not installed(store.data_dir):
        progress(0.02, "installing Demucs")
        install(store.data_dir, Path(python))
    work = store.data_dir / "tmp" / f"stems_{new_id('s')}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        src = work / "mix.f32"
        progress(0.08, "decoding the song")
        proc = procutil.run([exe, "-nostdin", "-y", "-loglevel", "error", "-i", str(store.data_dir / song["file_path"]),
                             "-vn", "-ac", "2", "-ar", str(SR), "-f", "f32le", str(src)], timeout=600)
        if proc.returncode != 0 or not src.is_file() or src.stat().st_size < SR * 8 // 10:
            raise StemsError("decode_failed", "the song could not be decoded")
        (work / "run.py").write_text(RUNNER, encoding="utf-8")
        dev = device or pick_device()
        progress(0.15, f"splitting on {dev}")
        proc = procutil.run([str(python), str(work / "run.py"), str(lib_dir(store.data_dir)), str(src),
                             str(src.stat().st_size // 8), str(work), dev, MODEL], text=True, timeout=3600)
        if proc.returncode != 0:
            raise StemsError("split_failed", f"Demucs failed: {(proc.stderr or '')[-600:]}")
        info = json.loads((proc.stdout or "{}").strip().splitlines()[-1])
        made: dict[str, str] = {}
        for i, stem in enumerate(info.get("stems") or []):
            raw = work / f"{stem}.f32"
            if stem not in STEMS or not raw.is_file():
                continue
            progress(0.85 + 0.03 * i, f"saving {stem}")
            aid = new_id("a")
            out = store.path_for_asset_file(aid, ".flac")
            enc = procutil.run([exe, "-nostdin", "-y", "-loglevel", "error", "-f", "f32le", "-ar", str(SR), "-ac", "2",
                                "-i", str(raw), "-c:a", "flac", str(out)], timeout=600)
            if enc.returncode != 0 or not out.is_file():
                raise StemsError("encode_failed", f"could not save the {stem} stem")
            peak = float(np.abs(np.fromfile(raw, dtype=np.float32)).max(initial=0.0))
            made[stem] = store.create_asset(
                project_id=song["project_id"], kind="audio", file_path=out.relative_to(store.data_dir).as_posix(),
                mime="audio/flac", duration_s=song.get("duration_s"), source="derived", asset_id=aid,
                name=f"{Path(song.get('name') or song_id).stem} - {stem}"[:100], tags=["stem", stem],
                recipe={"operation": "stems", "stem": stem, "model": MODEL, "device": info.get("device"),
                        "derived_from": song_id, "input_asset_ids": [song_id], "peak": round(peak, 4),
                        "backend": "local", "created_at": now_iso()})["id"]
        if not made:
            raise StemsError("split_failed", "Demucs gave no stems")
        if all(k in made for k in ("drums", "bass", "other")):
            progress(0.97, "mixing the instrumental")
            made["instrumental"] = mix_instrumental(store, song, made)
        return {"song_asset_id": song_id, "stems": made, "device": info.get("device"), "reused": False}
    finally:
        shutil.rmtree(work, ignore_errors=True)
