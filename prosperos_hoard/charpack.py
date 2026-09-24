"""`.hoardchar`: one portable file per character, and the global casting
library built on it.

A pack is a zip:

    character.json        the character (look, negative, palette, voice,
                          bio, role) + its kit (trigger, adapters, dataset
                          captions, sheet views, identity threshold, seeds,
                          history) with images referred to by pack path
    images/canonical.png  the canonical reference
    images/ref_NN.png     the other references
    sheet/<view>.png      model-sheet views
    dataset/NNN.png       dataset images (their captions live in the json)
    adapters/<file>       LoRA weights (optional: they can be large)
    preview.png           a contact sheet for file browsers and the library

Importing re-creates the character in any project (renamed on a name
clash), imports every image as an asset and installs adapters into
ComfyUI's loras folder when one is configured. The library keeps
`data/library/characters/<lib_id>/v<N>.hoardchar` plus `meta.json`, so a
character has versions: saving again after a change adds v2, v3... and any
version can be cast into a project.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Optional

from PIL import Image, ImageOps

from . import __version__
from . import charkit
from . import engine
from .backend import Backend
from .ids import new_id
from .store import NotFound, Store
from .util import now_iso

FORMAT = "hoardchar"
FORMAT_VERSION = 1
MAX_PACK_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB: a few adapters at most
MAX_JSON_BYTES = 4 * 1024 * 1024
_LIB_ID = re.compile(r"lib_[0-9A-Za-z]{10,40}")


class PackError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _png_bytes(path: Path, max_side: int = 2048) -> bytes:
    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGBA") if img.mode in ("RGBA", "LA", "P") else img.convert("RGB")
        img.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()


def _asset_path(store: Store, asset_id: Optional[str]) -> Optional[Path]:
    if not asset_id:
        return None
    try:
        a = store.get_asset(asset_id)
    except NotFound:
        return None
    p = store.data_dir / a["file_path"]
    return p if a["kind"] == "image" and p.is_file() else None


def build_pack(store: Store, character_id: str, dest: Path, *, include_dataset: bool = True,
               include_adapters: bool = True, lora_root: Optional[Path] = None) -> dict[str, Any]:
    """Write the character's pack to `dest`. Adapters are taken from the
    app's own copy (`data/adapters/`) or, for imported ones, from
    `lora_root/<lora_name>`; an adapter whose file is not found is listed
    without weights (`weights: false`)."""
    char = store.get_character(character_id)
    kit = charkit.kit_of(char)
    images: dict[str, Any] = {"canonical": None, "references": [], "sheet": [], "dataset": []}
    files: dict[str, bytes] = {}
    preview_paths: list[Path] = []
    canon = _asset_path(store, char.get("canonical_asset_id"))
    if canon:
        files["images/canonical.png"] = _png_bytes(canon)
        images["canonical"] = "images/canonical.png"
        preview_paths.append(canon)
    for i, aid in enumerate(r for r in char.get("reference_asset_ids") or [] if r != char.get("canonical_asset_id")):
        p = _asset_path(store, aid)
        if p:
            name = f"images/ref_{i + 1:02d}.png"
            files[name] = _png_bytes(p)
            images["references"].append(name)
    for aid in kit["sheet"].get("asset_ids") or []:
        p = _asset_path(store, aid)
        if not p:
            continue
        view = next((t[5:] for t in store.get_asset(aid).get("tags") or [] if t.startswith("view:")), None) or aid[-6:]
        name = f"sheet/{view}.png"
        n = 2
        while name in files:
            name = f"sheet/{view}_{n}.png"
            n += 1
        files[name] = _png_bytes(p)
        images["sheet"].append({"path": name, "view": view})
        if len(preview_paths) < 9:
            preview_paths.append(p)
    dataset_entries = []
    if include_dataset:
        for i, d in enumerate(kit["dataset"]):
            p = _asset_path(store, d["asset_id"])
            if not p:
                continue
            name = f"dataset/{i + 1:03d}.png"
            files[name] = _png_bytes(p, 1536)
            dataset_entries.append({"path": name, "caption": d.get("caption") or "", "include": d.get("include", True),
                                    **({"view": d["view"]} if d.get("view") else {}), "source": d.get("source")})
    images["dataset"] = dataset_entries
    adapters = []
    for a in kit["adapters"]:
        entry = {k: a.get(k) for k in ("arch", "lora_name", "strength", "trigger", "enabled", "source", "created_at",
                                       "trained", "eval", "notes") if a.get(k) is not None}
        src = None
        if include_adapters:
            if a.get("file_rel") and (store.data_dir / a["file_rel"]).is_file():
                src = store.data_dir / a["file_rel"]
            elif lora_root is not None and (lora_root / a["lora_name"]).is_file():
                src = lora_root / a["lora_name"]
        if src is not None:
            entry["path"] = f"adapters/{Path(a['lora_name']).name}"
            entry["weights"] = True
        else:
            entry["weights"] = False
        adapters.append((entry, src))
    doc = {
        "format": FORMAT, "version": FORMAT_VERSION, "exported_at": now_iso(), "app": f"prosperos-hoard {__version__}",
        "character": {k: char.get(k) for k in ("name", "role", "bio", "prompt", "negative", "palette", "voice", "notes")},
        "kit": {"trigger": kit["trigger"], "use_adapters": kit["use_adapters"], "identity": kit.get("identity"),
                "good_seeds": kit.get("good_seeds") or [], "history": kit.get("history") or [],
                "adapters": [e for e, _ in adapters], "sheet_views": kit["sheet"].get("views") or []},
        "images": images,
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("character.json", json.dumps(doc, ensure_ascii=False, indent=2))
        for name, data in files.items():
            z.writestr(name, data, compress_type=zipfile.ZIP_STORED)
        for entry, src in adapters:
            if src is not None:
                z.write(src, entry["path"], compress_type=zipfile.ZIP_STORED)
        if preview_paths:
            sheet = engine.contact_sheet(preview_paths[:9], cols=3, cell=256)
            buf = io.BytesIO()
            sheet.save(buf, format="PNG")
            z.writestr("preview.png", buf.getvalue(), compress_type=zipfile.ZIP_STORED)
    tmp.replace(dest)
    return {"path": dest, "bytes": dest.stat().st_size, "name": char["name"], "images": len(files),
            "dataset": len(dataset_entries), "adapters": sum(1 for e, _ in adapters if e["weights"]),
            "adapters_without_weights": [e["lora_name"] for e, _ in adapters if not e["weights"]]}


def _safe_member(name: str) -> bool:
    return bool(name) and not name.startswith(("/", "\\")) and ".." not in Path(name).parts and ":" not in name


def read_manifest(path: Path) -> dict[str, Any]:
    """character.json of a pack, validated."""
    if not path.is_file():
        raise PackError("not_found", "pack file not found")
    if path.stat().st_size > MAX_PACK_BYTES:
        raise PackError("too_big", "the pack is larger than 2 GB")
    try:
        with zipfile.ZipFile(path) as z:
            info = z.getinfo("character.json")
            if info.file_size > MAX_JSON_BYTES:
                raise PackError("bad_pack", "character.json is too large")
            doc = json.loads(z.read(info).decode("utf-8"))
            names = set(z.namelist())
    except KeyError:
        raise PackError("bad_pack", "not a .hoardchar pack (no character.json)") from None
    except (zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError):
        raise PackError("bad_pack", "not a .hoardchar pack (unreadable zip or json)") from None
    if doc.get("format") != FORMAT:
        raise PackError("bad_pack", "not a .hoardchar pack")
    if int(doc.get("version") or 0) > FORMAT_VERSION:
        raise PackError("newer_pack", f"this pack uses format v{doc.get('version')}; update Prospero's Hoard to read it")
    ch = doc.get("character") or {}
    if not isinstance(ch.get("name"), str) or not ch["name"].strip():
        raise PackError("bad_pack", "the pack's character has no name")
    for n in names:
        if not _safe_member(n):
            raise PackError("bad_pack", f"unsafe path inside the pack: {n[:80]}")
    doc["_members"] = sorted(names)
    return doc


def inspect_pack(path: Path) -> dict[str, Any]:
    doc = read_manifest(path)
    images = doc.get("images") or {}
    kit = doc.get("kit") or {}
    return {"name": doc["character"]["name"], "role": doc["character"].get("role"),
            "look": engine._clip(doc["character"].get("prompt"), 300), "exported_at": doc.get("exported_at"),
            "app": doc.get("app"), "trigger": kit.get("trigger"),
            "canonical": bool(images.get("canonical")), "references": len(images.get("references") or []),
            "sheet_views": [s.get("view") for s in images.get("sheet") or []],
            "dataset": len(images.get("dataset") or []),
            "adapters": [{k: a.get(k) for k in ("arch", "lora_name", "strength", "weights")} for a in kit.get("adapters") or []],
            "bytes": path.stat().st_size}


def _import_image(store: Store, project_id: str, data: bytes, name: str, tags: list[str]) -> str:
    asset_id = new_id("a")
    dest = store.path_for_asset_file(asset_id, ".png")
    dest.write_bytes(data)
    with Image.open(dest) as img:
        w, h = img.size
    engine.make_thumbnail(dest, store.path_for_thumb(asset_id))
    asset = store.create_asset(project_id=project_id, kind="image", file_path=engine._rel(store, dest), mime="image/png",
                               width=w, height=h, thumb_path=engine._rel(store, store.path_for_thumb(asset_id)),
                               source="import", asset_id=asset_id, name=name[:80],
                               recipe={"operation": "import_pack", "backend": "local", "created_at": now_iso()})
    if tags:
        store.update_asset(asset["id"], tags=tags)
    return asset["id"]


def import_pack(store: Store, backend: Optional[Backend], project_id: str, path: Path,
                rename: Optional[str] = None) -> dict[str, Any]:
    """Create the pack's character in `project_id`. Returns the new
    character id, name (a suffix is added on a clash), counts and notes."""
    doc = read_manifest(path)
    store.get_project(project_id)
    ch = doc["character"]
    kit_in = doc.get("kit") or {}
    images = doc.get("images") or {}
    existing = {c["name"].lower() for c in store.list_characters(project_id)}
    name = (rename or ch["name"]).strip()[:80]
    base, n = name, 2
    while name.lower() in existing:
        name = f"{base} {n}"
        n += 1
    notes: list[str] = []
    palette = [c for c in ch.get("palette") or [] if isinstance(c, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", c)][:12]
    voice = ch.get("voice") if isinstance(ch.get("voice"), dict) else None
    char = store.create_character(project_id, name, role=ch.get("role"), bio=ch.get("bio"), prompt=ch.get("prompt"),
                                  negative=ch.get("negative"), palette=palette, voice=voice, notes=ch.get("notes"))
    tag = charkit.char_tag(char["id"])
    canonical_id: Optional[str] = None
    refs: list[str] = []
    sheet_ids: list[str] = []
    dataset: list[dict[str, Any]] = []
    members = set(doc.get("_members") or [])
    with zipfile.ZipFile(path) as z:
        def read(member: Optional[str]) -> Optional[bytes]:
            if not member or member not in members:
                return None
            data = z.read(member)
            try:
                Image.open(io.BytesIO(data)).verify()
            except Exception:  # noqa: BLE001 - a broken image is skipped, not fatal
                notes.append(f"skipped unreadable image {member}")
                return None
            return data

        data = read(images.get("canonical"))
        if data:
            canonical_id = _import_image(store, project_id, data, f"{name} · canonical", [tag, "reference"])
        for member in images.get("references") or []:
            data = read(member)
            if data:
                refs.append(_import_image(store, project_id, data, f"{name} · reference", [tag, "reference"]))
        for entry in images.get("sheet") or []:
            data = read(entry.get("path"))
            if data:
                view = str(entry.get("view") or "view")[:30]
                sheet_ids.append(_import_image(store, project_id, data, f"{name} · {view}", [tag, "sheet", f"view:{view}"]))
        for entry in images.get("dataset") or []:
            data = read(entry.get("path"))
            if not data:
                continue
            aid = _import_image(store, project_id, data, f"{name} · dataset", [tag, "dataset"])
            dataset.append({"asset_id": aid, "caption": str(entry.get("caption") or "")[:600],
                            "include": bool(entry.get("include", True)), "source": entry.get("source") or "pack",
                            **({"view": entry["view"]} if entry.get("view") else {})})
        char = store.update_character(char["id"], canonical_asset_id=canonical_id, reference_asset_ids=refs) \
            if canonical_id or refs else char
        kit = charkit.kit_of(char)
        trig = kit_in.get("trigger")
        if isinstance(trig, str) and re.fullmatch(r"[A-Za-z0-9_]{3,32}", trig):
            kit["trigger"] = trig
        kit["use_adapters"] = bool(kit_in.get("use_adapters", True))
        if isinstance(kit_in.get("identity"), dict):
            kit["identity"] = {"threshold": float((kit_in["identity"]).get("threshold", 6.5))}
        kit["good_seeds"] = [s for s in kit_in.get("good_seeds") or [] if isinstance(s, int)][-50:]
        kit["history"] = [h for h in kit_in.get("history") or [] if isinstance(h, dict)][-charkit.HISTORY_CAP:]
        kit["dataset"] = dataset[:charkit.MAX_DATASET]
        if sheet_ids:
            kit["sheet"] = {"asset_ids": sheet_ids, "views": [str(v) for v in kit_in.get("sheet_views") or []]}
        installed_adapters = 0
        for a in kit_in.get("adapters") or []:
            arch = a.get("arch")
            if arch not in charkit.trainers_mod.ARCH_PRESETS or not a.get("lora_name"):
                continue
            lora_name = str(a["lora_name"]).replace("\\", "/")
            installed = False
            file_rel = None
            member = a.get("path")
            if a.get("weights") and member in members:
                keep_dir = store.data_dir / "adapters"
                keep_dir.mkdir(parents=True, exist_ok=True)
                fname = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(member).name)[:120]
                keep = keep_dir / fname
                if keep.exists():
                    keep = keep_dir / f"{keep.stem}_{new_id('x')[-6:]}{keep.suffix}"
                with z.open(member) as src, open(keep, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                file_rel = keep.relative_to(store.data_dir).as_posix()
                if backend is not None:
                    lora_name, installed = charkit.install_adapter_file(backend, keep, fname)
                if installed:
                    installed_adapters += 1
                else:
                    notes.append(f"adapter {fname} kept in data/adapters; set Training -> LoRA folder to install it")
            else:
                notes.append(f"adapter {lora_name} came without weights; it is used only if ComfyUI already has that file")
                installed = True  # trust the name; resolve_adapters checks ComfyUI's list at render time
            kit["adapters"].append({"id": new_id("ad"), "arch": arch, "lora_name": lora_name,
                                    "strength": float(a.get("strength") or 1.0), "trigger": a.get("trigger") or kit["trigger"],
                                    "enabled": bool(a.get("enabled", True)), "installed": installed, "source": "pack",
                                    "created_at": a.get("created_at") or now_iso(),
                                    **({"file_rel": file_rel} if file_rel else {}),
                                    **({"trained": a["trained"]} if isinstance(a.get("trained"), dict) else {})})
        charkit.save_kit(store, char["id"], kit, "imported", f"from pack {path.name}")
    return {"character_id": char["id"], "name": name, "canonical_asset_id": canonical_id, "references": len(refs),
            "sheet": len(sheet_ids), "dataset": len(dataset), "adapters": len(kit["adapters"]),
            "adapters_installed": installed_adapters, "notes": notes}


# ------------------------------------------------------------- library --

def library_root(data_dir: Path) -> Path:
    return Path(data_dir) / "library" / "characters"


def _meta_path(data_dir: Path, lib_id: str) -> Path:
    if not _LIB_ID.fullmatch(lib_id or ""):
        raise PackError("unknown_library_entry", f"'{str(lib_id)[:60]}' is not a library id")
    return library_root(data_dir) / lib_id / "meta.json"


def _read_meta(data_dir: Path, lib_id: str) -> dict[str, Any]:
    p = _meta_path(data_dir, lib_id)
    if not p.is_file():
        raise PackError("unknown_library_entry", f"no library character {lib_id}")
    return json.loads(p.read_text(encoding="utf-8"))


def library_list(data_dir: Path, query: Optional[str] = None) -> list[dict[str, Any]]:
    root = library_root(data_dir)
    out = []
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        meta_p = d / "meta.json"
        if not meta_p.is_file():
            continue
        try:
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if query and query.lower() not in (meta.get("name", "") + " " + (meta.get("look") or "")).lower():
            continue
        latest = (meta.get("versions") or [{}])[-1]
        out.append({"id": meta["id"], "name": meta.get("name"), "role": meta.get("role"),
                    "look": engine._clip(meta.get("look"), 160), "version": latest.get("v"),
                    "versions": len(meta.get("versions") or []), "updated_at": latest.get("at"),
                    "adapters": latest.get("adapters") or [], "has_preview": (d / "preview.png").is_file(),
                    "origin": meta.get("origin")})
    out.sort(key=lambda m: m.get("updated_at") or "", reverse=True)
    return out


def library_save(store: Store, backend: Optional[Backend], character_id: str, note: Optional[str] = None,
                 include_adapters: bool = True) -> dict[str, Any]:
    """Save the character as a new version in the library (creating the
    entry on first save). The kit remembers {library: {id, version}}."""
    char = store.get_character(character_id)
    kit = charkit.kit_of(char)
    lib = kit.get("library") or {}
    data_dir = store.data_dir
    lib_id = lib.get("id")
    meta: dict[str, Any]
    if lib_id:
        try:
            meta = _read_meta(data_dir, lib_id)
        except PackError:
            lib_id = None
    if not lib_id:
        lib_id = new_id("lib")
        meta = {"id": lib_id, "name": char["name"], "versions": [],
                "origin": {"project_id": char["project_id"], "character_id": char["id"]}}
    folder = library_root(data_dir) / lib_id
    folder.mkdir(parents=True, exist_ok=True)
    version = len(meta["versions"]) + 1
    pack = folder / f"v{version}.hoardchar"
    lroot = charkit.lora_dir(backend) if backend is not None else None
    built = build_pack(store, character_id, pack, include_adapters=include_adapters, lora_root=lroot)
    try:
        with zipfile.ZipFile(pack) as z:
            if "preview.png" in z.namelist():
                (folder / "preview.png").write_bytes(z.read("preview.png"))
    except zipfile.BadZipFile:
        pass
    prev = meta["versions"][-1] if meta["versions"] else None
    changes = _diff_summary(prev, char, kit)
    meta.update({"name": char["name"], "role": char.get("role"), "look": char.get("prompt")})
    meta["versions"].append({"v": version, "at": now_iso(), "note": (note or "")[:300], "bytes": built["bytes"],
                             "adapters": [f"{a['arch']}:{Path(a['lora_name']).name}" for a in kit["adapters"]],
                             "look": char.get("prompt"), "canonical": bool(char.get("canonical_asset_id")),
                             "dataset": built["dataset"], "changes": changes})
    _meta_path(data_dir, lib_id).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    kit["library"] = {"id": lib_id, "version": version}
    charkit.save_kit(store, character_id, kit, "library_saved", f"{lib_id} v{version}")
    return {"id": lib_id, "version": version, "bytes": built["bytes"], "changes": changes,
            "adapters_without_weights": built["adapters_without_weights"]}


def _diff_summary(prev: Optional[dict[str, Any]], char: dict[str, Any], kit: dict[str, Any]) -> list[str]:
    if prev is None:
        return ["first version"]
    out = []
    if (prev.get("look") or "") != (char.get("prompt") or ""):
        out.append("look changed")
    adapters = [f"{a['arch']}:{Path(a['lora_name']).name}" for a in kit["adapters"]]
    if adapters != prev.get("adapters"):
        out.append("adapters changed")
    if len(kit["dataset"]) != prev.get("dataset"):
        out.append(f"dataset {prev.get('dataset')} -> {len(kit['dataset'])}")
    return out or ["no visible change"]


def library_pack_path(data_dir: Path, lib_id: str, version: Optional[int] = None) -> Path:
    meta = _read_meta(data_dir, lib_id)
    versions = meta.get("versions") or []
    if not versions:
        raise PackError("empty_library_entry", f"{lib_id} has no saved version")
    v = int(version) if version else versions[-1]["v"]
    if not any(x["v"] == v for x in versions):
        raise PackError("unknown_version", f"{meta.get('name')} has versions {', '.join(str(x['v']) for x in versions)}")
    return library_root(data_dir) / lib_id / f"v{v}.hoardchar"


def library_use(store: Store, backend: Optional[Backend], project_id: str, lib_id: str,
                version: Optional[int] = None, rename: Optional[str] = None) -> dict[str, Any]:
    result = import_pack(store, backend, project_id, library_pack_path(store.data_dir, lib_id, version), rename)
    char = store.get_character(result["character_id"])
    kit = charkit.kit_of(char)
    kit["library"] = {"id": lib_id, "version": int(version) if version else _read_meta(store.data_dir, lib_id)["versions"][-1]["v"]}
    charkit.save_kit(store, char["id"], kit, "cast_from_library", f"{lib_id} v{kit['library']['version']}")
    return result


def library_history(data_dir: Path, lib_id: str) -> dict[str, Any]:
    meta = _read_meta(data_dir, lib_id)
    return {"id": lib_id, "name": meta.get("name"), "versions": meta.get("versions") or [], "origin": meta.get("origin")}


def library_delete(data_dir: Path, lib_id: str) -> dict[str, Any]:
    _read_meta(data_dir, lib_id)
    shutil.rmtree(library_root(data_dir) / lib_id, ignore_errors=True)
    return {"deleted": lib_id}


def export_to(store: Store, backend: Optional[Backend], character_id: str, *, include_dataset: bool = True,
              include_adapters: bool = True) -> dict[str, Any]:
    """Export into `data/exports/characters/<name>.hoardchar` (served by id
    through the API; the agent never gets a path)."""
    char = store.get_character(character_id)
    slug = re.sub(r"[^a-z0-9]+", "-", char["name"].lower()).strip("-")[:40] or "character"
    dest = store.data_dir / "exports" / "characters" / f"{slug}-{char['id'][-6:].lower()}.hoardchar"
    lroot = charkit.lora_dir(backend) if backend is not None else None
    built = build_pack(store, character_id, dest, include_dataset=include_dataset, include_adapters=include_adapters,
                       lora_root=lroot)
    built["file"] = dest.name
    built.pop("path")
    return built


def export_path(data_dir: Path, file_name: str) -> Path:
    if not re.fullmatch(r"[a-z0-9-]{1,60}\.hoardchar", file_name or ""):
        raise PackError("bad_file", "unknown export")
    p = Path(data_dir) / "exports" / "characters" / file_name
    if not p.is_file():
        raise PackError("not_found", "export not found; export the character again")
    return p


def save_upload(data_dir: Path, data: bytes) -> Path:
    """An uploaded pack goes to `data/tmp/` first (validated from there)."""
    if len(data) > MAX_PACK_BYTES:
        raise PackError("too_big", "the pack is larger than 2 GB")
    tmp_dir = Path(data_dir) / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(suffix=".hoardchar", dir=tmp_dir)
    with open(fd, "wb") as f:
        f.write(data)
    return Path(name)
