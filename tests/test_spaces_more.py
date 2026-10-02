"""Spaces, second round: variations, pose and layout references, a clip
that ends on a frame, long lip sync, groups, estimates before a run, a
space used as an app, and the assistant that draws a graph."""

from __future__ import annotations

import json

import pytest

from prosperos_hoard import spaces
from test_spaces import FakeStudio, _space_with, _store


def _plan(client, graph, node):
    store, sid = _space_with(client, graph)
    sp = store.get_space(sid)
    return spaces.plan_node(store, sp["graph"], sp["state"], node), store, sid


def test_variations_make_one_edit_per_change(client):
    graph = {"nodes": [{"id": "a", "type": "asset", "data": {"kind": "image", "asset_ids": []}},
                       {"id": "v", "type": "variations", "data": {"mode": "expressions", "count": 3}}],
             "edges": [{"source": "a", "target": "v", "target_handle": "image"}]}
    store, sid = _space_with(client, graph)
    store.patch_space_state(sid, "a", {})
    sp = store.get_space(sid)
    sp["graph"]["nodes"][0]["data"]["asset_ids"] = ["a_img"]
    with pytest.raises(spaces.SpaceError):
        spaces.plan_node(store, {**sp["graph"], "nodes": [sp["graph"]["nodes"][1]], "edges": []}, sp["state"], "v")
    # an asset node's ids are looked up: use a generator's outputs instead
    g2 = {"nodes": [{"id": "p", "type": "image", "data": {"prompt": "x"}},
                    {"id": "v", "type": "variations", "data": {"mode": "expressions", "count": 3}}],
          "edges": [{"source": "p", "target": "v", "target_handle": "image"}]}
    store2, sid2 = _space_with(client, g2)
    store2.patch_space_state(sid2, "p", {"outputs": ["a_pic"]})
    sp2 = store2.get_space(sid2)
    ops = spaces.plan_node(store2, sp2["graph"], sp2["state"], "v")
    assert len(ops) == 3 and all(o["body"]["reference_asset_ids"] == ["a_pic"] for o in ops)
    assert "a warm smile" in ops[0]["body"]["prompt"] and "<image1>" in ops[0]["body"]["prompt"]


def test_pose_and_layout_go_in_as_numbered_references(client):
    g = {"nodes": [{"id": "ref", "type": "image", "data": {"prompt": "hero"}},
                   {"id": "pose", "type": "edit", "data": {"operation": "pose_map"}},
                   {"id": "pic", "type": "image", "data": {"prompt": "the hero dancing"}}],
         "edges": [{"source": "ref", "target": "pic", "target_handle": "refs"},
                   {"source": "ref", "target": "pose", "target_handle": "image"},
                   {"source": "pose", "target": "pic", "target_handle": "pose"}]}
    store, sid = _space_with(client, g)
    store.patch_space_state(sid, "ref", {"outputs": ["a_ref"]})
    store.patch_space_state(sid, "pose", {"outputs": ["a_pose"]})
    sp = store.get_space(sid)
    body = spaces.plan_node(store, sp["graph"], sp["state"], "pic")[0]["body"]
    assert body["reference_asset_ids"] == ["a_ref", "a_pose"]
    assert "<image2> is a pose map" in body["prompt"]
    assert spaces.plan_node(store, sp["graph"], sp["state"], "pose")[0]["body"]["operation"] == "pose_map"


def test_a_clip_with_an_end_frame_and_a_long_sung_line(client):
    g = {"nodes": [{"id": "a", "type": "image", "data": {"prompt": "x"}}, {"id": "b", "type": "image", "data": {"prompt": "y"}},
                   {"id": "s", "type": "music", "data": {"tags": "pop"}},
                   {"id": "v", "type": "video", "data": {"prompt": "go"}},
                   {"id": "sing", "type": "video", "data": {"seconds": 40, "sing_engine": "infinitetalk"}}],
         "edges": [{"source": "a", "target": "v", "target_handle": "start"},
                   {"source": "b", "target": "v", "target_handle": "end"},
                   {"source": "a", "target": "sing", "target_handle": "start"},
                   {"source": "s", "target": "sing", "target_handle": "audio"}]}
    store, sid = _space_with(client, g)
    for nid, out in (("a", "a_a"), ("b", "a_b"), ("s", "a_s")):
        store.patch_space_state(sid, nid, {"outputs": [out]})
    sp = store.get_space(sid)
    v = spaces.plan_node(store, sp["graph"], sp["state"], "v")[0]["body"]
    assert v["end_asset_id"] == "a_b" and v["template"] == "auto_clip"
    sing = spaces.plan_node(store, sp["graph"], sp["state"], "sing")[0]["body"]
    assert sing["template"] == "auto_sing" and sing["audio_seconds"] == 40
    assert sing["template_params"] == {"sing_engine": "infinitetalk"}
    with pytest.raises(spaces.SpaceError):  # one end frame only
        spaces.validate_graph({**g, "edges": g["edges"] + [{"source": "a", "target": "v", "target_handle": "end"}]})


def test_groups_are_plain_frames():
    g = spaces.validate_graph({"nodes": [{"id": "g", "type": "group", "w": 600, "h": 400, "data": {"title": "Act 1"}},
                                         {"id": "t", "type": "text"}], "edges": []})
    assert g["nodes"][0]["w"] == 600 and "g" not in spaces.run_order(g, "all")
    with pytest.raises(spaces.SpaceError):
        spaces.validate_graph({"nodes": [{"id": "g", "type": "group"}, {"id": "p", "type": "image"}],
                               "edges": [{"source": "g", "target": "p", "target_handle": "refs"}]})


def test_estimate_uses_this_computers_render_times(client):
    c, _, _ = client
    g = {"nodes": [{"id": "p", "type": "image", "data": {"prompt": "x", "count": 2}},
                   {"id": "v", "type": "video", "data": {"prompt": "go"}}],
         "edges": [{"source": "p", "target": "v", "target_handle": "start"}]}
    store, sid = _space_with(client, g)
    est = c.get(f"/api/spaces/{sid}/estimate").json()
    by = {n["node"]: n for n in est["nodes"]}
    assert by["p"]["renders"] == 2 and by["p"]["seconds"] == 60 * 2 and not by["p"]["measured"]
    # no start picture yet: counted from what the picture node will give it
    assert by["v"]["rough"] and by["v"]["renders"] == 2 and by["v"]["templates"] == ["wan22_i2v_14b"]
    assert est["renders"] == 4 and est["seconds"] == 60 * 2 + 300 * 2


def test_a_space_runs_as_an_app(client, fake_comfy):
    c, _, _ = client
    pid = c.post("/api/projects", json={"name": "App"}).json()["id"]
    sp = c.post(f"/api/projects/{pid}/spaces", json={"name": "Poster maker"}).json()
    graph = {"nodes": [{"id": "subject", "type": "text", "data": {"text": "a cat", "app_input": True, "app_label": "Subject"}},
                       {"id": "style", "type": "text", "data": {"text": "STYLE: risograph poster"}},
                       {"id": "poster", "type": "image", "data": {"aspect": "4:5", "app_output": True}}],
             "edges": [{"source": "subject", "target": "poster", "target_handle": "prompt"},
                       {"source": "style", "target": "poster", "target_handle": "prompt"}]}
    c.put(f"/api/spaces/{sp['id']}", json={"graph": graph})
    app_ = c.get(f"/api/spaces/{sp['id']}/app").json()
    assert [f["label"] for f in app_["inputs"]] == ["Subject"] and [o["node"] for o in app_["outputs"]] == ["poster"]
    bad = c.post(f"/api/spaces/{sp['id']}/app/run", json={"values": {"style": "x"}})
    assert bad.status_code == 400
    run = c.post(f"/api/spaces/{sp['id']}/app/run", json={"values": {"subject": "a red fox"}}).json()
    from test_spaces import _wait_job
    _wait_job(c, run["job"]["id"], 120)
    after = c.get(f"/api/spaces/{sp['id']}/app").json()
    assert after["inputs"][0]["value"] == "a red fox" and after["outputs"][0]["outputs"]
    first = c.get(f"/api/assets/{after['outputs'][0]['outputs'][0]}").json()
    assert first["recipe"]["prompt"].startswith("a red fox")
    listed = c.post(f"/api/agent/studio_spaces?project={pid}", json={"action": "list"}).json()
    assert listed["items"][0]["app"] is True
    via_agent = c.post(f"/api/agent/studio_spaces?project={pid}", json={"action": "app", "space": sp["id"]}).json()
    assert via_agent["inputs"][0]["node"] == "subject"


def test_the_assistant_draws_a_graph(client):
    c, app, _ = client
    pid = c.post("/api/projects", json={"name": "Build"}).json()["id"]
    c.post(f"/api/agent/studio_cast?project={pid}", json={"action": "create", "name": "Vera", "fields": {"prompt": "a singer"}})
    sp = c.post(f"/api/projects/{pid}/spaces", json={"name": "b"}).json()
    plan = {"note": "Two shots of Vera", "nodes": [
        {"id": "vera", "type": "cast", "name": "Vera"},
        {"id": "style", "type": "text", "text": "STYLE: 35mm"},
        {"id": "shot1", "type": "image", "prompt": "Vera on stage", "aspect": "16:9", "prompt_from": ["style", "vera"], "refs": ["vera"]},
        {"id": "clip1", "type": "video", "prompt": "she sings", "start": ["shot1"]},
        {"id": "clip2", "type": "video", "prompt": "the crowd cheers", "after": "clip1"},
        {"id": "film", "type": "combine", "clips": ["clip1", "clip2"]},
        {"id": "ghost", "type": "cast", "name": "Nobody"},
        {"id": "bad", "type": "spaceship"}]}
    replies = iter(["not json at all", "```json\n" + json.dumps(plan) + "\n```"])
    app.state.short_hooks = {**(getattr(app.state, "short_hooks", None) or {}), "chat": lambda m, t, temp: next(replies)}
    r = c.post(f"/api/spaces/{sp['id']}/build", json={"request": "two shots of Vera singing, joined"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert set(out["added"]) == {"vera", "style", "shot1", "clip1", "clip2", "film"} and out["note"]
    g = c.get(f"/api/spaces/{sp['id']}").json()["graph"]
    edges = {(e["source"], e["source_handle"], e["target"], e["target_handle"]) for e in g["edges"]}
    assert ("vera", "text", "shot1", "prompt") in edges and ("vera", "image", "shot1", "refs") in edges
    assert ("clip1", "last", "clip2", "start") in edges and ("clip2", None, "film", "clips") in edges
    xs = {n["id"]: n["x"] for n in g["nodes"]}
    assert xs["vera"] < xs["shot1"] < xs["clip1"] < xs["clip2"] < xs["film"]
    app.state.short_hooks = {**app.state.short_hooks, "chat": lambda m, t, temp: "nothing"}
    assert c.post(f"/api/spaces/{sp['id']}/build", json={"request": "x"}).status_code == 400


def test_the_build_uses_the_cast_and_the_projects_song(client):
    from PIL import Image
    from test_lipsync import _song
    from test_qa import _save
    c, app, _ = client
    store = app.state.store
    pid = c.post("/api/projects", json={"name": "Build cast"}).json()["id"]
    face = _save(store, pid, Image.new("RGB", (64, 64), (200, 180, 160)))
    made = c.post(f"/api/agent/studio_cast?project={pid}", json={"action": "create", "name": "Vera",
                                                                 "fields": {"prompt": "a singer"}}).json()
    vid = made.get("id") or made.get("character", {}).get("id")
    c.post(f"/api/agent/studio_cast?project={pid}", json={"action": "update", "id": vid, "fields": {"canonical_asset_id": face}})
    song = _song(store, {"id": pid}, 4.0)
    sp = c.post(f"/api/projects/{pid}/spaces", json={"name": "b"}).json()
    seen = {}
    plan = {"note": "ok", "nodes": [
        {"id": "sheet", "type": "image", "prompt": "Character reference sheet for VERA, front and back", "sheet": True},
        {"id": "a1", "type": "image", "prompt": "Wide shot of VERA on stage", "refs": ["sheet"]},
        {"id": "a2", "type": "image", "prompt": "Close-up of vera singing"},
        {"id": "v1", "type": "video", "prompt": "VERA sways", "start": ["a1"]},
        {"id": "tune", "type": "asset", "asset_id": song},
        {"id": "film", "type": "combine", "clips": ["v1"], "audio": "tune"}]}

    def chat(messages, t, temp):
        seen["ask"] = messages[-1]["content"]
        return json.dumps(plan)

    app.state.short_hooks = {**(getattr(app.state, "short_hooks", None) or {}), "chat": chat}
    r = c.post(f"/api/spaces/{sp['id']}/build", json={"request": "VERA on stage, with my song"})
    assert r.status_code == 200, r.text
    assert song in seen["ask"]  # the project's songs are offered
    g = c.get(f"/api/spaces/{sp['id']}").json()["graph"]
    nodes = {n["id"]: n for n in g["nodes"]}
    assert "sheet" not in nodes  # no new sheet for someone already in the cast
    cast = next(n for n in g["nodes"] if n["type"] == "cast")
    assert cast["data"]["character_id"] == vid
    edges = {(e["source"], e["source_handle"], e["target"], e["target_handle"]) for e in g["edges"]}
    assert (cast["id"], "image", "a1", "refs") in edges and (cast["id"], "image", "a2", "refs") in edges
    assert nodes["a1"]["data"]["prompt"] == "Wide shot of @Vera on stage" and "@Vera" in nodes["a2"]["data"]["prompt"]
    assert nodes["v1"]["data"]["prompt"] == "VERA sways"
    assert nodes["tune"]["data"] == {"kind": "audio", "asset_ids": [song]} and ("tune", None, "film", "audio") in edges


def test_an_unknown_at_name_becomes_plain_words(client):
    c, app, _ = client
    pid = c.post("/api/projects", json={"name": "No cast"}).json()["id"]
    sp = c.post(f"/api/projects/{pid}/spaces", json={"name": "b"}).json()
    plan = {"nodes": [{"id": "s", "type": "image", "prompt": "Reference sheet for @VERA", "sheet": True},
                      {"id": "i", "type": "image", "prompt": "@VERA on stage", "refs": ["s"]}]}
    app.state.short_hooks = {**(getattr(app.state, "short_hooks", None) or {}), "chat": lambda m, t, temp: json.dumps(plan)}
    assert c.post(f"/api/spaces/{sp['id']}/build", json={"request": "Vera on stage"}).status_code == 200
    g = c.get(f"/api/spaces/{sp['id']}").json()["graph"]
    assert {n["id"]: n["data"]["prompt"] for n in g["nodes"]}["i"] == "VERA on stage"
