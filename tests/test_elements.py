"""Places and objects in the cast: @mentionable like characters, with a
look and a reference image that goes in as a numbered reference on
Qwen-Image 2.1, so a stage or a guitar stays the same shot to shot."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from prosperos_hoard import engine


def _upload_png(c, project_id: str, colour=(200, 60, 90)) -> str:
    buf = io.BytesIO()
    Image.new("RGB", (64, 96), colour).save(buf, "PNG")
    r = c.post(f"/api/projects/{project_id}/import-upload", files={"file": ("ref.png", buf.getvalue(), "image/png")})
    r.raise_for_status()
    return r.json()["id"]


def _cast(c, pid, kind, name, **fields):
    r = c.post(f"/api/agent/studio_cast?project={pid}", json={"action": "create", "kind": kind, "name": name, "fields": fields})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def scene(client):
    c, _, _ = client
    pid = c.post("/api/projects", json={"name": "Elements"}).json()["id"]
    aria_img, stage_img = _upload_png(c, pid), _upload_png(c, pid, (20, 20, 160))
    aria = _cast(c, pid, "character", "Aria", prompt="a singer with silver hair", canonical_asset_id=aria_img)
    stage = _cast(c, pid, "location", "Neon Stage", prompt="a small neon-lit club stage with a brick wall",
                  canonical_asset_id=stage_img)
    guitar = _cast(c, pid, "prop", "Red Guitar", prompt="a cherry red hollow-body guitar")
    return c, pid, {"aria": aria, "stage": stage, "guitar": guitar, "aria_img": aria_img, "stage_img": stage_img}


def test_places_and_objects_are_cast_entries_with_an_element(scene):
    c, pid, s = scene
    assert s["stage"]["element"] == "location" and s["guitar"]["element"] == "prop" and "element" not in s["aria"]
    items = c.get(f"/api/projects/{pid}/characters").json()["items"]
    assert sorted(i["element"] for i in items) == ["character", "location", "prop"]
    r = c.post(f"/api/agent/studio_cast?project={pid}",
               json={"action": "create", "kind": "group", "name": "Band", "fields": {"member_ids": [s["stage"]["id"]]}})
    assert r.status_code >= 400 and "not a character" in r.text


def test_an_empty_place_renders_from_its_reference(scene):
    c, pid, s = scene
    body = c.post(f"/api/agent/studio_generate_image?project={pid}",
                  json={"prompt": "the empty @Neon Stage at dawn", "wait_s": 20}).json()
    assert body["template"] == "qwen21_edit" and body["matched_elements"] == ["Neon Stage"], body
    assert "a small neon-lit club stage" in body["final_prompt"]
    assert "<image1>: the place Neon Stage" in body["final_prompt"]
    assert body["job"]["state"] == "done", body
    asset = c.get(f"/api/assets/{body['job']['asset_ids'][0]}").json()
    assert asset["recipe"]["input_asset_ids"] == [s["stage_img"]]


def test_element_references_can_be_turned_off(scene):
    c, pid, _ = scene
    body = c.post(f"/api/agent/studio_generate_image?project={pid}",
                  json={"prompt": "the empty @Neon Stage at dawn", "use_element_references": False, "wait_s": 0}).json()
    assert body["template"] == "qwen21_txt2img", body


def test_a_consistent_shot_numbers_the_place_after_the_character(scene):
    c, pid, s = scene
    body = c.post(f"/api/agent/studio_generate_image?project={pid}",
                  json={"prompt": "@Aria singing on @Neon Stage with @Red Guitar", "consistent": True, "wait_s": 20}).json()
    assert body["template"] == "qwen21_edit", body
    prompt = body["final_prompt"]
    assert prompt.startswith("Keep the character from <image1>")
    assert "set it in the place from <image2>" in prompt
    assert "the character from <image1> singing on the place from <image2> with a cherry red hollow-body guitar" in prompt
    assert body["matched_characters"] == ["Aria"] and body["matched_elements"] == ["Neon Stage", "Red Guitar"]
    asset = c.get(f"/api/assets/{body['job']['asset_ids'][0]}").json()
    assert asset["recipe"]["input_asset_ids"] == [s["aria_img"], s["stage_img"]]


def test_the_place_follows_the_callers_own_references(scene):
    c, pid, s = scene
    extra = _upload_png(c, pid, (0, 200, 0))
    body = c.post(f"/api/agent/studio_generate_image?project={pid}",
                  json={"prompt": "@Aria on @Neon Stage", "consistent": True, "reference_asset_ids": [extra],
                        "wait_s": 20}).json()
    assert "the place from <image3>" in body["final_prompt"], body
    asset = c.get(f"/api/assets/{body['job']['asset_ids'][0]}").json()
    assert asset["recipe"]["input_asset_ids"] == [s["aria_img"], extra, s["stage_img"]]


def test_a_single_reference_engine_inlines_the_others(store, project):
    a = store.create_character(project["id"], "Aria", prompt="a singer with silver hair")
    store.create_character(project["id"], "Stage", prompt="a neon club stage", element="location")
    store.create_character(project["id"], "Mika", prompt="a drummer with a fox mask")
    k = engine.build_kontext_instruction(store, project["id"], "@Aria and @Mika on @Stage", engine="flux")
    assert k["reference_asset_ids"] == [] and k["matched_characters"] == ["Aria", "Mika"]
    assert k["matched_elements"] == ["Stage"]
    # Aria has no image: nothing to keep from a reference, her look is inlined like the others
    assert "a singer with silver hair and a drummer with a fox mask on a neon club stage" in k["instruction"]
    assert a["element"] == "character"


def test_element_must_be_known(store, project):
    with pytest.raises(ValueError):
        store.create_character(project["id"], "Thing", element="vehicle")


def test_the_planner_is_offered_the_projects_places_and_objects(client):
    c, app, _ = client
    pid = c.post("/api/projects", json={"name": "Planned"}).json()["id"]
    lead = _cast(c, pid, "character", "Aria", prompt="a singer")
    _cast(c, pid, "location", "Neon Stage", prompt="a small neon-lit club stage")
    _cast(c, pid, "prop", "Red Guitar", prompt="a cherry red guitar")
    seen = {}

    def chat(messages, max_tokens, temperature):
        seen["user"] = messages[-1]["content"]
        return ('{"title": "T", "world_look": "neon", "world_negative": "text", "song": {"tags": "pop", "lyrics": '
                '"[Verse]\\nla la", "bpm": 120, "key": "C major"}, "shots": [{"prompt": "@Aria on @Neon Stage", '
                '"lead": true, "motion": "move", "motion_prompt": "push in", "section": "verse"}, {"prompt": '
                '"close-up of @Red Guitar", "lead": false, "motion": "still", "motion_prompt": "", "section": "chorus"}]}')

    app.state.short_hooks = {**(getattr(app.state, "short_hooks", None) or {}), "chat": chat}
    r = c.post("/api/agent/studio_video_plan", json={"concept": "a gig", "character_id": lead["id"], "shots": 2})
    assert r.status_code == 200, r.text
    assert r.json()["elements"] == ["Neon Stage", "Red Guitar"]
    assert "- @Neon Stage (place): a small neon-lit club stage" in seen["user"]
    assert "- @Red Guitar (object): a cherry red guitar" in seen["user"]
    assert "@Aria" not in seen["user"].split("Places and objects")[1].split("Plan 2 shots")[0]


def test_a_cast_entry_is_deleted_recoverably(scene):
    c, pid, s = scene
    stage = s["stage"]["id"]
    r = c.delete(f"/api/characters/{stage}")
    assert r.status_code == 200 and r.json()["deleted"] == stage
    live = [i["name"] for i in c.get(f"/api/projects/{pid}/characters").json()["items"]]
    assert "Neon Stage" not in live
    assert [i["name"] for i in c.get(f"/api/projects/{pid}/characters/deleted").json()["items"]] == ["Neon Stage"]
    body = c.post(f"/api/agent/studio_generate_image?project={pid}", json={"prompt": "an empty @Neon Stage"}).json()
    assert body["unknown_mentions"] == ["Neon"], body  # no longer answers to its @mention
    assert c.get(f"/api/characters/{stage}").json()["name"] == "Neon Stage"  # lineage still resolves it
    r = c.post(f"/api/agent/studio_cast?project={pid}", json={"action": "restore", "id": stage})
    assert r.status_code == 200 and r.json()["name"] == "Neon Stage"
    assert c.get(f"/api/projects/{pid}/characters/deleted").json()["items"] == []


def test_a_restore_never_makes_two_of_the_same_name(scene):
    c, pid, s = scene
    c.delete(f"/api/characters/{s['guitar']['id']}")
    _cast(c, pid, "prop", "Red Guitar", prompt="another guitar")
    r = c.post(f"/api/characters/{s['guitar']['id']}/restore")
    assert r.status_code >= 400 and "already has" in r.text


def test_a_deleted_member_leaves_its_group_and_comes_back(scene):
    c, pid, s = scene
    mika = _cast(c, pid, "character", "Mika", prompt="a drummer")
    group = c.post(f"/api/agent/studio_cast?project={pid}", json={
        "action": "create", "kind": "group", "name": "Duo", "fields": {"member_ids": [s["aria"]["id"], mika["id"]]}}).json()
    c.delete(f"/api/characters/{mika['id']}")
    assert c.get(f"/api/projects/{pid}/groups").json()["items"][0]["member_ids"] == [s["aria"]["id"]]
    c.post(f"/api/characters/{mika['id']}/restore")
    assert c.get(f"/api/projects/{pid}/groups").json()["items"][0]["member_ids"] == [s["aria"]["id"], mika["id"]]
    r = c.delete(f"/api/groups/{group['id']}")
    assert r.status_code == 200 and c.get(f"/api/projects/{pid}/groups").json()["items"] == []


def test_the_lead_of_an_unfinished_production_needs_force(scene, data_dir):
    from prosperos_hoard import productions as prod
    from test_productions import tiny_spec
    c, pid, s = scene
    spec = tiny_spec()
    spec["lead"] = {"character_id": s["aria"]["id"], "name": "Aria", "look": "a singer"}
    prod.create_production(data_dir, "Gig", prod.normalise_spec(spec), {}, project_id=pid)
    r = c.delete(f"/api/characters/{s['aria']['id']}")
    assert r.status_code == 400 and r.json()["error"] == "in_use" and "Gig" in r.json()["message"]
    assert c.delete(f"/api/characters/{s['aria']['id']}?force=true").status_code == 200


def test_a_characters_negative_reaches_a_consistent_edit(store, project):
    store.create_character(project["id"], "Ball", prompt="a clown with a polka-dot ball head",
                           negative="face, eyes, mouth")
    k = engine.build_kontext_instruction(store, project["id"], "@Ball dancing", engine="qwen21")
    assert k["instruction"].endswith("Do not add: face, eyes, mouth") and k["negative_extra"] == "face, eyes, mouth"
