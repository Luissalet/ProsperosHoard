"""Receive a versioned handoff as a project, reference assets and an unqueued draft."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from . import engine, productions
from .store import Store


SCHEMA = "hoard.production-dossier/1"


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _receipt(store: Store, state: dict[str, Any]) -> str:
    return _digest([store.get_project(state["project_id"]), state])


def receive(store: Store, backend: Any, dossier_json: str, reference_files: dict[str, str]) -> dict[str, Any]:
    try:
        dossier = json.loads(dossier_json)
        if not isinstance(dossier, dict) or dossier.get("schema") != SCHEMA:
            raise ValueError(f"expected {SCHEMA}")
        title = dossier["title"].strip()
        if not title or dossier["subject"]["kind"] not in ("scene", "character", "place"):
            raise ValueError("give a title and a scene, character or place")
        keep = dossier["preserveDetails"]
        if not isinstance(keep, list) or not all(isinstance(detail, str) for detail in keep):
            raise ValueError("preserveDetails must be a list of strings")
        refs = {ref["id"]: ref for ref in dossier["references"]}
        if any(key not in refs or not refs[key]["available"] for key in reference_files):
            raise ValueError("every file must match an available reference in the dossier")
        scene = dossier["scene"]
        style = dossier["world"]["visualStyle"]
        characters = dossier["characters"]
        prompt = "\n\n".join(part for part in [
            scene["text"].strip() or scene["title"], scene.get("setting"), style,
            "\n".join(f"- {detail}" for detail in keep),
        ] if part)
        manifest = _json(dossier)
    except (ValueError, TypeError, AttributeError, KeyError) as exc:
        raise engine.EngineError("bad_dossier", f"Invalid production dossier: {exc}") from exc

    paths = {key: engine.resolve_import_path(backend, store, path) for key, path in reference_files.items()}
    with_hashes = {key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in paths.items()}
    slug = "dossier-" + _digest([manifest, with_hashes])[:32]
    with productions.lock_for(slug):
        if (productions.production_dir(store.data_dir, slug) / "state.json").is_file():
            state = productions.load_state(store.data_dir, slug)
            return _result(store, state, existing=True)

        project = store.create_project(title, prompt)
        pid = project["id"]
        try:
            assets = {}
            for key, path in paths.items():
                source = refs[key]
                name = source.get("name") or path.name
                assets[key] = engine.import_asset(
                    store, pid, path, "image", original_name=f"{name}{path.suffix}" if not name.lower().endswith(path.suffix.lower()) else name,
                    recipe={"operation": "production_dossier", "source_ref": source["ref"]},
                )["id"]
            lead = characters[0] if len(characters) == 1 and dossier["subject"]["kind"] != "place" else None
            lead_look = "\n".join(filter(None, [lead["text"], *lead["fields"].values()])) if lead else ""
            lead_spec = {"name": (lead["title"] if lead else title)[:80], "look": (lead_look or prompt)[:2000]}
            if lead:
                lead_ref_ids = [asset_id for key, asset_id in assets.items() if any(
                    owner["entityId"] == lead["id"] for owner in refs[key]["owners"]
                )]
                canonical = next((assets[key] for key in assets if any(
                    owner["entityId"] == lead["id"] and owner["role"] == "canonical" for owner in refs[key]["owners"]
                )), None)
                char = store.create_character(pid, lead_spec["name"], prompt=lead_spec["look"], bio=lead["text"][:2000],
                                              reference_asset_ids=lead_ref_ids, canonical_asset_id=canonical)
                lead_spec["character_id"] = char["id"]
            else:
                canonical = None
            usable = [asset_id for asset_id in assets.values() if asset_id != canonical][:6]
            state = productions.create_production(store.data_dir, title, {
                "title": title,
                "lead": lead_spec,
                "world": {"look": style or ""},
                "shots": [{"key": "1", "prompt": prompt, "lead": lead is not None,
                           "refs": [{"asset_id": asset_id} for asset_id in usable]}],
                "production_dossier": dossier,
                "dossier_reference_assets": assets,
            }, project_id=pid, slug=slug)
        except Exception:
            store.trash_project(pid)
            productions.stash_productions(store.data_dir, pid)
            raise
        return _result(store, state, existing=False)


def _result(store: Store, state: dict[str, Any], *, existing: bool) -> dict[str, Any]:
    spec = state["spec"]
    return {"ok": True, "production": state["slug"], "project_id": state["project_id"],
            "existing": existing, "queued": False, "preserveDetails": spec["production_dossier"]["preserveDetails"],
            "character_id": spec["lead"].get("character_id"), "reference_assets": spec["dossier_reference_assets"],
            **({} if existing else {"undo_receipt": _receipt(store, state)})}


def discard(store: Store, project: str, production: str, receipt: str, *, dry_run: bool = False) -> dict[str, Any]:
    if not re.fullmatch(r"dossier-[a-f0-9]{32}", production):
        raise engine.EngineError("bad_dossier", "give a production dossier id")
    with productions.lock_for(production):
        if store.get_project(project).get("deleted_at"):
            return {"already_in_trash": project}
        state = productions.load_state(store.data_dir, production)
        if state["project_id"] != project or state["spec"].get("production_dossier", {}).get("schema") != SCHEMA:
            raise engine.EngineError("bad_dossier", "the production is not the project dossier")
        if _receipt(store, state) != receipt or store.project_live_jobs(project):
            raise engine.EngineError("conflict", "the project or production changed after this handoff")
        if dry_run:
            return {"would_trash_project": project, "production": production}
        store.trash_project(project)
        return {"trashed_project": project, "productions_moved": productions.stash_productions(store.data_dir, project)}
