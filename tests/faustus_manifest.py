"""Validate a `faustus-plugin.json` the way Faustus reads it (manifest schema 1).

Faustus refuses a manifest with an unknown key, a missing `mcp.command`, a
transport other than stdio, or an id that is not alphanumeric with - or _.
A refused manifest is logged and skipped, so a typo here means the plugin is
silently invisible. This module mirrors those rules so a test in this
repository fails first.

Mirror of `src/plugins.py::parse_manifest` in the Faustus repository.
Keep in step with it when the schema grows.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

SCHEMA = 1
TOP_LEVEL_KEYS = {
    "schema", "id", "name", "purpose", "capabilities",
    "app", "mcp", "placeholders", "defaults", "provides", "notes",
}
APP_KEYS = {"url_default", "ui_url", "health", "identify", "launch_hint"}
MCP_KEYS = {"transport", "command", "args", "env", "optional_env"}
HEALTH_KEYS = {"path", "expect"}
PROVIDES_KEYS = {"skills", "recipes", "knowledge"}


class ManifestError(ValueError):
    pass


def _unknown(obj: Any, allowed: set, where: str) -> None:
    if not isinstance(obj, dict):
        raise ManifestError(f"{where} must be an object")
    extra = sorted(set(obj) - allowed)
    if extra:
        raise ManifestError(f"{where} has unknown key(s): {', '.join(extra)}")


def _str_list(value: Any, where: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ManifestError(f"{where} must be a list of strings")
    return list(value)


def _str_map(value: Any, where: str) -> Dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise ManifestError(f"{where} must be an object of string to string")
    return dict(value)


def validate(data: Any) -> Dict[str, Any]:
    """Raise ManifestError with a reason, or return the manifest unchanged."""
    _unknown(data, TOP_LEVEL_KEYS, "manifest")
    schema = data.get("schema")
    if not isinstance(schema, int) or isinstance(schema, bool) or schema < 1:
        raise ManifestError("'schema' must be a positive integer")
    if schema > SCHEMA:
        raise ManifestError(f"schema {schema} is newer than {SCHEMA}")
    plugin_id = data.get("id")
    if not isinstance(plugin_id, str) or not plugin_id.strip():
        raise ManifestError("manifest has no 'id'")
    if not all(c.isalnum() or c in "-_" for c in plugin_id.strip()):
        raise ManifestError(f"'id' must be alphanumeric with - or _: {plugin_id!r}")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ManifestError("manifest has no 'name'")
    if not isinstance(data.get("purpose") or "", str):
        raise ManifestError("'purpose' must be a string")
    _str_list(data.get("capabilities"), "'capabilities'")
    placeholders = _str_list(data.get("placeholders"), "'placeholders'")
    _str_map(data.get("defaults"), "'defaults'")

    app = data.get("app") or {}
    _unknown(app, APP_KEYS, "'app'")
    if not isinstance(app.get("url_default") or "", str):
        raise ManifestError("'app.url_default' must be a string")
    if app.get("ui_url") is not None and not isinstance(app.get("ui_url"), str):
        raise ManifestError("'app.ui_url' must be a string or null")
    health = app.get("health") or {}
    _unknown(health, HEALTH_KEYS, "'app.health'")
    if not isinstance(health.get("path") or "", str):
        raise ManifestError("'app.health.path' must be a string")
    if not isinstance(health.get("expect") or {}, dict):
        raise ManifestError("'app.health.expect' must be an object")
    identify = app.get("identify") or {}
    if not isinstance(identify, dict):
        raise ManifestError("'app.identify' must be an object")
    for key, value in identify.items():
        _str_list(value, f"'app.identify.{key}'")
    if not isinstance(app.get("launch_hint") or {}, dict):
        raise ManifestError("'app.launch_hint' must be an object")

    mcp = data.get("mcp") or {}
    _unknown(mcp, MCP_KEYS, "'mcp'")
    if (mcp.get("transport") or "stdio") != "stdio":
        raise ManifestError("'mcp.transport' must be 'stdio'")
    command = mcp.get("command") or ""
    if not isinstance(command, str) or not command.strip():
        raise ManifestError("'mcp.command' is required")
    args = _str_list(mcp.get("args"), "'mcp.args'")
    _str_map(mcp.get("env"), "'mcp.env'")
    _str_list(mcp.get("optional_env"), "'mcp.optional_env'")

    provides = data.get("provides") or {}
    _unknown(provides, PROVIDES_KEYS, "'provides'")
    if not isinstance(data.get("notes") or "", str):
        raise ManifestError("'notes' must be a string")

    # Faustus fills `{X_DIR}` from the running app's working directory only
    # when an mcp arg names a file that exists under it. Without that the
    # connect form comes up with the directory blank.
    dir_keys = [p for p in placeholders if p.endswith("_DIR")]
    if dir_keys and not any("{" + dir_keys[0] + "}" in a for a in args):
        raise ManifestError(f"no mcp arg is anchored on {{{dir_keys[0]}}}")
    return data


def check_repo(root: Path) -> Dict[str, Any]:
    """Validate `<root>/faustus-plugin.json` and that the files it names exist."""
    path = root / "faustus-plugin.json"
    data = validate(json.loads(path.read_text(encoding="utf-8")))
    dir_keys = [p for p in data.get("placeholders", []) if p.endswith("_DIR")]
    if dir_keys:
        token = "{" + dir_keys[0] + "}"
        for arg in data["mcp"].get("args", []):
            if arg.startswith(token):
                rel = arg[len(token):].lstrip("/\\")
                if not (root / rel).is_file():
                    raise ManifestError(f"mcp arg points at a missing file: {rel}")
    for skill in (data.get("provides") or {}).get("skills", []) or []:
        if isinstance(skill, str) and not (root / skill).is_file():
            raise ManifestError(f"provides.skills names a missing file: {skill}")
    return data
