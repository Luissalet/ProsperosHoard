"""Character kit: adapters, model sheet, dataset, training, takes, packs,
library - end to end over the fake ComfyUI and the fake trainer."""

from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from prosperos_hoard import charkit, charpack, comfy_driver, identity
from prosperos_hoard.workflows import convert as convert_mod


def _project(c, name="Kit Test"):
    return c.post("/api/agent/studio_create_project", json={"name": name}).json()["id"]


def _job(c, job_id, timeout=60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        j = c.get(f"/api/agent/studio_job?job_id={job_id}").json()
        if j["state"] in ("done", "failed", "cancelled"):
            return j
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} did not finish: {j}")


def _character(c, pid, name="Farol", look="a paper lantern creature with a warm orange glow"):
    job = c.post(f"/api/agent/studio_generate_image?project={pid}",
                 json={"prompt": f"portrait of {look}", "template": "flux_schnell_txt2img", "seed": 7, "wait_s": 30}).json()["job"]
    assert job["state"] == "done", job
    made = c.post(f"/api/agent/studio_cast?project={pid}",
                  json={"action": "create", "name": name,
                        "fields": {"prompt": look, "canonical_asset_id": job["asset_ids"][0]}}).json()
    return made["id"], job["asset_ids"][0]


def _training(c, data_dir: Path):
    loras = data_dir / "fake_comfy" / "loras"
    loras.mkdir(parents=True, exist_ok=True)
    r = c.post("/api/training", json={"action": "settings", "training": {
        "lora_dir": str(loras), "trainers": [{"kind": "fake", "name": "fake", "delay": 0.001}]}})
    assert r.status_code == 200, r.text
    return loras


# ------------------------------------------------------------------ unit --

def test_inject_loras_rewires_every_model_consumer():
    wf, spec = comfy_driver.load_template("qwen21_txt2img")
    wf = comfy_driver.apply_params(wf, spec, {"positive_prompt": "x"})
    comfy_driver.inject_loras(wf, [{"name": "a.safetensors", "strength": 0.9}, {"name": "b.safetensors"}])
    loader = next(k for k, n in wf.items() if n["class_type"] == "UNETLoader")
    chain = [k for k, n in wf.items() if n["class_type"] == "LoraLoaderModelOnly"]
    assert len(chain) == 2
    assert wf[chain[0]]["inputs"]["model"] == [loader, 0] and wf[chain[0]]["inputs"]["strength_model"] == 0.9
    assert wf[chain[1]]["inputs"]["model"] == [chain[0], 0]
    sampler = next(n for n in wf.values() if n["class_type"] == "KSampler")
    assert sampler["inputs"]["model"] == [chain[1], 0]
    assert convert_mod.validate_converted(wf, {}) is not None  # still a structurally valid graph


def test_inject_loras_on_checkpoint_templates_and_noop():
    wf, spec = comfy_driver.load_template("sdxl_txt2img")
    before = json.dumps(wf, sort_keys=True)
    comfy_driver.inject_loras(wf, [])
    assert json.dumps(wf, sort_keys=True) == before
    comfy_driver.inject_loras(wf, [{"name": "x.safetensors"}])
    ck = next(k for k, n in wf.items() if n["class_type"] == "CheckpointLoaderSimple")
    # CLIP (output 1) and VAE (output 2) stay on the checkpoint
    assert any(v == [ck, 1] for n in wf.values() for v in n["inputs"].values() if isinstance(v, list))
    assert comfy_driver.template_arch("wan22_ti2v") == "wan22_5b"
    assert comfy_driver.template_arch("custom", {"arch": "flux1"}) == "flux1"


def test_triggers_and_caption_cleaning():
    assert charkit.default_trigger("Farol Ñu") == "farolnuchr"
    assert charkit.with_triggers("a lantern", ["farolchr"]) == "farolchr, a lantern"
    assert charkit.with_triggers("farolchr at night", ["farolchr"]) == "farolchr at night"
    char = {"name": "Farol", "prompt": "a paper lantern"}
    assert charkit.clean_caption("@Farol a paper lantern walking in rain", char, "farolchr") == "farolchr, walking in rain"


def test_identity_rough_score_separates_colours():
    red = Image.new("RGB", (200, 200), (230, 40, 40))
    red2 = Image.new("RGB", (200, 200), (220, 50, 45))
    blue = Image.new("RGB", (200, 200), (40, 60, 230))
    assert identity.rough_score(red2, [red]) > 8
    assert identity.rough_score(blue, [red]) < 2
    assert identity.parse_identity('sure {"identity": 7.5, "why": "same hat"}') == (7.5, "same hat")


# ------------------------------------------------------------------- api --

def test_adapters_attach_resolve_and_inject(client, data_dir):
    c, app, _ = client
    pid = _project(c)
    cid, _ = _character(c, pid)
    store = app.state.store
    # a LoRA file ComfyUI lists
    loras = data_dir / "fake_comfy" / "loras"
    (loras / "prospero").mkdir(parents=True)
    (loras / "prospero" / "farol_flux.safetensors").write_bytes(b"x")
    r = c.post("/api/agent/studio_character_adapters", json={"character_id": cid, "action": "available"})
    assert "prospero/farol_flux.safetensors" in r.json()["loras"]
    r = c.post("/api/agent/studio_character_adapters",
               json={"character_id": cid, "action": "attach", "lora_name": "prospero/farol_flux.safetensors",
                     "arch": "flux1", "strength": 0.8})
    assert r.status_code == 200, r.text
    adapter_id = r.json()["adapter"]["id"]
    body = c.post(f"/api/agent/studio_generate_image?project={pid}",
                  json={"prompt": "@Farol in the rain", "template": "flux_schnell_txt2img", "wait_s": 30}).json()
    assert body["adapters"][0]["lora_name"] == "prospero/farol_flux.safetensors"
    assert body["final_prompt"].startswith("farolchr, ")
    asset = store.get_asset(body["job"]["asset_ids"][0])
    assert asset["recipe"]["params"]["loras"] == [{"name": "prospero/farol_flux.safetensors", "strength": 0.8}]
    # another architecture: not injected
    body = c.post(f"/api/agent/studio_generate_image?project={pid}",
                  json={"prompt": "@Farol in the rain", "template": "sdxl_txt2img", "wait_s": 0}).json()
    assert "adapters" not in body
    # opt out
    body = c.post(f"/api/agent/studio_generate_image?project={pid}",
                  json={"prompt": "@Farol", "template": "flux_schnell_txt2img", "use_adapters": False}).json()
    assert "adapters" not in body and not body["final_prompt"].startswith("farolchr")
    # a file ComfyUI does not list is skipped with a note
    c.post("/api/agent/studio_character_adapters",
           json={"character_id": cid, "action": "update", "adapter_id": adapter_id, "enabled": False})
    c.post("/api/agent/studio_character_adapters",
           json={"character_id": cid, "action": "attach", "lora_name": "prospero/gone.safetensors", "arch": "flux1"})
    body = c.post(f"/api/agent/studio_generate_image?project={pid}",
                  json={"prompt": "@Farol", "template": "flux_schnell_txt2img"}).json()
    assert "adapters" not in body and "not in ComfyUI" in body["adapter_notes"][0]
    kit = c.post("/api/agent/studio_character_adapters", json={"character_id": cid, "action": "list"}).json()
    assert [a["enabled"] for a in kit["adapters"]] == [False, True]
    bad = c.post("/api/agent/studio_character_adapters",
                 json={"character_id": cid, "action": "settings", "settings": {"trigger": "no spaces allowed"}})
    assert bad.status_code == 400 and bad.json()["error"] == "bad_trigger"


def test_sheet_dataset_takes_training_end_to_end(client, data_dir):
    c, app, _ = client
    store = app.state.store
    pid = _project(c)
    cid, canonical = _character(c, pid)
    loras = _training(c, data_dir)

    # model sheet
    r = c.post("/api/agent/studio_character_sheet",
               json={"character_id": cid, "views": ["front", "profile", "back", "closeup", "happy", "scared",
                                                    "action", "sitting"], "wait_s": 60})
    assert r.status_code == 200, r.text
    job = r.json()["job"]
    if job["state"] != "done":
        job = _job(c, job["id"])
    assert job["state"] == "done", job
    kit = charkit.kit_of(store.get_character(cid))
    assert len(kit["sheet"]["asset_ids"]) == 8 and kit["sheet"]["contact_sheet_id"]
    views = {t for a in kit["sheet"]["asset_ids"] for t in store.get_asset(a)["tags"] if t.startswith("view:")}
    assert "view:profile" in views
    assert all(d["caption"].startswith("farolchr, ") for d in kit["dataset"])

    # dataset
    ds = c.post("/api/agent/studio_character_dataset",
                json={"character_id": cid, "action": "build", "sources": ["canonical", "sheet"]}).json()
    assert ds["report"]["images"] == 9
    first = ds["items"][0]["asset_id"]
    ds = c.post("/api/agent/studio_character_dataset",
                json={"character_id": cid, "action": "update",
                      "items": [{"asset_id": first, "caption": "farolchr, glowing softly"}]}).json()
    assert ds["items"][0]["caption"] == "farolchr, glowing softly"
    cap = c.post("/api/agent/studio_character_dataset", json={"character_id": cid, "action": "caption", "wait_s": 20}).json()
    assert cap["job"]["state"] == "done", cap

    # takes
    takes = c.post("/api/agent/studio_character_takes", json={"character_id": cid}).json()
    assert takes["total"] >= 8
    scored = c.post("/api/agent/studio_character_takes", json={"character_id": cid, "action": "score", "wait_s": 30}).json()
    assert scored["job"]["state"] == "done"
    takes = c.post("/api/agent/studio_character_takes", json={"character_id": cid, "sort": "identity"}).json()
    assert any(t["identity"] is not None for t in takes["items"])
    target = next(t["asset_id"] for t in takes["items"] if not t["is_canonical"])
    r = c.post("/api/agent/studio_character_takes",
               json={"character_id": cid, "action": "act", "asset_id": target, "take_action": "reference"})
    assert r.status_code == 200 and target in store.get_character(cid)["reference_asset_ids"]
    c.post("/api/agent/studio_character_takes", json={"character_id": cid, "action": "act", "asset_id": target,
                                                      "take_action": "reject"})
    assert "take:rejected" in store.get_asset(target)["tags"]
    assert target not in [d["asset_id"] for d in charkit.kit_of(store.get_character(cid))["dataset"]]

    # training (fake trainer)
    trainers = c.post("/api/agent/studio_character_train", json={"action": "trainers"}).json()
    assert trainers["trainers"][0]["ok"] and trainers["lora_dir_ok"]
    plan = c.post("/api/agent/studio_character_train",
                  json={"action": "plan", "character_id": cid, "arch": "flux1", "overrides": {"steps": 120}}).json()
    assert plan["ready"] and plan["plan"]["steps"] == 120 and plan["trainer"] == "fake"
    r = c.post("/api/agent/studio_character_train",
               json={"action": "start", "character_id": cid, "arch": "flux1", "overrides": {"steps": 120}})
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    job = _job(c, r.json()["job"]["id"])
    assert job["state"] == "done", job
    kit = charkit.kit_of(store.get_character(cid))
    adapter = kit["adapters"][-1]
    assert adapter["source"] == "trained" and adapter["installed"] and adapter["arch"] == "flux1"
    assert (loras / adapter["lora_name"]).is_file()
    log = c.post("/api/agent/studio_character_train", json={"action": "log", "run_id": run_id}).json()
    assert any("/120" in line for line in log["lines"])
    status = c.post("/api/agent/studio_character_train", json={"action": "status", "character_id": cid}).json()
    assert status["runs"][0]["run_id"] == run_id
    # the trained adapter is used from now on
    body = c.post(f"/api/agent/studio_generate_image?project={pid}",
                  json={"prompt": "@Farol dancing", "template": "flux_schnell_txt2img", "wait_s": 30}).json()
    assert body["adapters"][0]["lora_name"] == adapter["lora_name"]
    assert body["job"]["state"] == "done"
    # prefer_adapter + consistent: txt2img with the adapter instead of the canonical edit
    body = c.post(f"/api/agent/studio_generate_image?project={pid}",
                  json={"prompt": "@Farol dancing", "engine": "flux", "consistent": True, "prefer_adapter": True}).json()
    assert body.get("route") == "adapter" and body["template"] == "flux_schnell_txt2img"
    body = c.post(f"/api/agent/studio_generate_image?project={pid}",
                  json={"prompt": "@Farol dancing", "engine": "flux", "consistent": True}).json()
    assert body["template"] == "flux_kontext_edit" and body["adapters"]  # edit + adapter together


def test_training_refuses_without_trainer_or_dataset(client):
    c, _, _ = client
    pid = _project(c)
    cid, _ = _character(c, pid)
    plan = c.post("/api/agent/studio_character_train", json={"action": "plan", "character_id": cid, "arch": "flux1"})
    assert plan.status_code == 400 and plan.json()["error"] == "too_few_images"
    c.post("/api/agent/studio_character_dataset", json={"character_id": cid, "action": "build"})
    r = c.post("/api/training", json={"action": "settings", "training": {"trainers": [{"kind": "rocket"}]}})
    assert r.status_code == 400


def test_pack_roundtrip_and_library_versions(client, data_dir, tmp_path):
    c, app, allowed = client
    store = app.state.store
    pid = _project(c, "Source")
    cid, _ = _character(c, pid)
    loras = _training(c, data_dir)
    (loras / "prospero").mkdir(parents=True, exist_ok=True)
    (loras / "prospero" / "farol_q.safetensors").write_bytes(b"weights")
    c.post("/api/agent/studio_character_adapters",
           json={"character_id": cid, "action": "attach", "lora_name": "prospero/farol_q.safetensors", "arch": "qwen_image"})
    c.post("/api/agent/studio_character_dataset", json={"character_id": cid, "action": "build"})

    exported = c.post("/api/agent/studio_character_pack", json={"action": "export", "character_id": cid}).json()
    assert exported["adapters"] == 1 and "path" not in exported
    r = c.get(exported["download"])
    assert r.status_code == 200
    pack = allowed / "farol.hoardchar"
    pack.write_bytes(r.content)
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        names = z.namelist()
        doc = json.loads(z.read("character.json"))
    assert "images/canonical.png" in names and "adapters/farol_q.safetensors" in names and "preview.png" in names
    assert doc["format"] == "hoardchar" and doc["kit"]["trigger"] == "farolchr"

    info = c.post("/api/agent/studio_character_pack", json={"action": "inspect", "path": str(pack)}).json()
    assert info["name"] == "Farol" and info["adapters"][0]["weights"]
    other = _project(c, "Target")
    c.post(f"/api/agent/studio_cast?project={other}", json={"action": "create", "name": "Farol", "fields": {}})
    imp = c.post(f"/api/agent/studio_character_pack?project={other}", json={"action": "import", "path": str(pack)}).json()
    assert imp["name"] == "Farol 2" and imp["canonical_asset_id"] and imp["adapters"] == 1
    new = store.get_character(imp["character_id"])
    assert new["prompt"] == store.get_character(cid)["prompt"]
    assert charkit.kit_of(new)["trigger"] == "farolchr"
    # UI upload route
    r = c.post(f"/api/projects/{other}/character-packs", files={"file": ("f.hoardchar", pack.read_bytes())},
               params={"rename": "Farol Copy"})
    assert r.status_code == 200 and r.json()["name"] == "Farol Copy"
    bad = c.post(f"/api/projects/{other}/character-packs", files={"file": ("f.hoardchar", b"not a zip")})
    assert bad.status_code == 400 and bad.json()["error"] == "bad_pack"

    # library: two versions, cast the first into a project
    v1 = c.post("/api/agent/studio_character_library", json={"action": "save", "character_id": cid, "note": "first"}).json()
    c.post(f"/api/agent/studio_cast?project={pid}", json={"action": "update", "id": cid,
                                                          "fields": {"prompt": "a paper lantern, now with a crack"}})
    v2 = c.post("/api/agent/studio_character_library", json={"action": "save", "character_id": cid}).json()
    assert v1["id"] == v2["id"] and v2["version"] == 2 and "look changed" in v2["changes"]
    listed = c.post("/api/agent/studio_character_library", json={"action": "list"}).json()["items"]
    assert listed[0]["versions"] == 2
    used = c.post(f"/api/agent/studio_character_library?project={other}",
                  json={"action": "use", "id": v1["id"], "version": 1, "rename": "Old Farol"}).json()
    assert store.get_character(used["character_id"])["prompt"] == "a paper lantern creature with a warm orange glow"
    casting = c.post("/api/agent/studio_character_library", json={"action": "use", "id": v1["id"]}).json()
    assert store.get_project(store.get_character(casting["character_id"])["project_id"])["name"] == "Casting"
    assert c.get(f"/api/library/characters/{v1['id']}/preview").status_code == 200
    hist = c.post("/api/agent/studio_character_library", json={"action": "history", "id": v1["id"]}).json()
    assert [v["v"] for v in hist["versions"]] == [1, 2]


def test_pack_rejects_unsafe_members(tmp_path):
    bad = tmp_path / "evil.hoardchar"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("character.json", json.dumps({"format": "hoardchar", "version": 1, "character": {"name": "X"}}))
        z.writestr("../escape.png", b"x")
    with pytest.raises(charpack.PackError) as exc:
        charpack.read_manifest(bad)
    assert exc.value.code == "bad_pack"
    newer = tmp_path / "newer.hoardchar"
    with zipfile.ZipFile(newer, "w") as z:
        z.writestr("character.json", json.dumps({"format": "hoardchar", "version": 99, "character": {"name": "X"}}))
    with pytest.raises(charpack.PackError) as exc:
        charpack.read_manifest(newer)
    assert exc.value.code == "newer_pack"
