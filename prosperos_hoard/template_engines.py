"""Resolve registered render engines without starting them during discovery."""

from __future__ import annotations

from contextlib import contextmanager

from .hoard_link import _hubclient


DEPTH_TEMPLATE = "qwen21_depth"
DEPTH_PATCH = "qwen_image_2.1_fun_controlnet_union_int8_convrot.safetensors"
DEPTH_CONTROL = "QwenImageDiffsynthControlnet"


def _registration(template, *, replacement=False):
    args = {"templates": [template]}
    required = ["directory", "python", "port"]
    if template == DEPTH_TEMPLATE:
        args["models"] = [f"models/model_patches/{DEPTH_PATCH}"]
    else:
        required.append("models")
    if replacement:
        required.append("name")
    else:
        args["name"] = "qwen-depth" if template == DEPTH_TEMPLATE else template
    return {"tool": "hub_comfy_profile_register", "arguments": args, "required": required}


def _node_inputs(node):
    inputs = node.get("input", {})
    return {**inputs.get("required", {}), **inputs.get("optional", {})}


def depth_requirements(info):
    loader = _node_inputs(info.get("ModelPatchLoader", {})).get("name") or []
    choices = loader[0] if loader and isinstance(loader[0], list) else []
    missing = []
    if DEPTH_PATCH not in choices:
        missing.append(f"ModelPatchLoader: {DEPTH_PATCH}")
    if "end_percent" not in _node_inputs(info.get(DEPTH_CONTROL, {})):
        missing.append(f"{DEPTH_CONTROL}.end_percent")
    return missing


def _requirements_at(url):
    info = {}
    for node in ("ModelPatchLoader", DEPTH_CONTROL):
        code, data = _hubclient.fetch(f"{url}/object_info/{node}", timeout=3)
        if code == 200 and isinstance(data, dict):
            info.update(data)
    return depth_requirements(info)


def _profiles():
    _, found = _hubclient.fetch(_hubclient.hub_url() + "/api/services/comfy-profiles", timeout=4)
    return found.get("items", []) if isinstance(found, dict) else []


def template_profile(template, profiles=None):
    """Return the best registered engine for a template and its next tool call."""
    if profiles is None:
        profiles = _profiles()
    candidates = [row for row in profiles if template in row.get("profile", {}).get("templates", [])]
    preferred = {"ready": 0, "down": 1, "starting": 2}
    results = []
    for row in sorted(candidates, key=lambda candidate: candidate["id"]):
        profile = row["profile"]
        result = {key: row.get(key) for key in ("id", "url", "state", "gpu", "pid", "problem")}
        result["version"] = profile["version"]
        names = {model["name"] for model in profile["models"]}
        missing = [entry["name"] for entry in profile["models"] if not entry["available"]]
        if template == DEPTH_TEMPLATE and DEPTH_PATCH not in names:
            missing.append(DEPTH_PATCH)
        if row["problem"] or missing:
            result.update(state="incompatible", missing=missing, next=_registration(template, replacement=True))
        elif row["state"] == "running" and row["pid"]:
            missing = _requirements_at(row["url"]) if template == DEPTH_TEMPLATE else []
            result.update(state="incompatible" if missing else "ready", missing=missing,
                          next=_registration(template, replacement=True) if missing else None)
        else:
            result.update(state="port_in_use" if row["state"] == "running" else row["state"], missing=[],
                          next={"tool": "hub_service_start", "arguments": {"id": row["id"], "wait_s": 60}})
        results.append(result)
    return min(results, key=lambda row: preferred.get(row["state"], 3), default=None)


def template_status(default_url=None):
    profiles = _profiles()
    template_ids = {template for row in profiles for template in row.get("profile", {}).get("templates", [])}
    result = {template: template_profile(template, profiles) for template in sorted(template_ids)}
    if DEPTH_TEMPLATE not in result:
        missing = _requirements_at(default_url) if default_url else depth_requirements({})
        result[DEPTH_TEMPLATE] = {"state": "unregistered" if missing else "ready", "url": default_url,
                                  "missing": missing, "next": _registration(DEPTH_TEMPLATE) if missing else None}
    return result


def unavailable(template, profile):
    from .hoard_link.errors import Unavailable
    detail = ", ".join(profile.get("missing", [])) or profile.get("problem") or profile["state"]
    call = profile["next"]
    next_step = f". {call['tool']}({call['arguments']})" if call else ""
    return Unavailable("image", [f"{template}: {detail}{next_step}"])


def start_profile(template, profile, gpu, lease_id):
    """Start the selected endpoint on the GPU reserved for this render."""
    if gpu is None or not lease_id:
        raise unavailable(template, {**profile, "problem": "A GPU lease is required / Hace falta una reserva de GPU"})
    _, result = _hubclient.fetch(_hubclient.hub_url() + "/api/services/start", {
        "id": profile["id"], "gpu": gpu, "work_lease_id": lease_id, "wait_s": 90,
    }, timeout=105)
    if isinstance(result, dict) and result.get("ok"):
        current = template_profile(template)
        if current and current["id"] == profile["id"] and current["state"] == "ready" and current["gpu"] == gpu:
            return current
    problem = result.get("error") if isinstance(result, dict) else None
    problem = problem or "Service did not become ready / El servicio no ha quedado listo"
    raise unavailable(template, {**profile, "problem": problem})


@contextmanager
def bind_profile(backend, profile):
    """Temporarily bind both render preflight and execution to one endpoint."""
    bound = backend._bound
    old_url = getattr(bound, "url", None)
    old_profile = getattr(bound, "profile", None)
    bound.url, bound.profile = profile["url"], profile
    try:
        yield
    finally:
        bound.url, bound.profile = old_url, old_profile
