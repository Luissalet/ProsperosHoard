"""Spaces: the node canvas. Graph validation, input resolution, runs that
feed one generator's outputs into the next, skipping unchanged nodes,
unticking outputs, agent edits and templates."""

from __future__ import annotations

import time

import pytest

from prosperos_hoard import spaces


def _wait_job(c, job_id, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        j = c.get(f"/api/jobs/{job_id}").json()
        if j["state"] in ("done", "failed", "cancelled"):
            return j
        time.sleep(0.3)
    raise AssertionError("job did not finish")


def _space(c, pid, template="blank"):
    r = c.post(f"/api/projects/{pid}/spaces", json={"name": "Test", "template": template})
    assert r.status_code == 200, r.text
    return r.json()


def test_graph_validation_rejects_bad_wires():
    ok = spaces.validate_graph({"nodes": [{"id": "t", "type": "text", "data": {"text": "a"}},
                                          {"id": "i", "type": "image"}],
                                "edges": [{"source": "t", "target": "i", "target_handle": "prompt"}]})
    assert ok["edges"][0]["target_handle"] == "prompt"
    bad = [
        {"nodes": [{"id": "t", "type": "text"}, {"id": "i", "type": "image"}],
         "edges": [{"source": "t", "target": "i", "target_handle": "refs"}]},          # text into an image input
        {"nodes": [{"id": "i", "type": "image"}, {"id": "v", "type": "video"}],
         "edges": [{"source": "i", "target": "v", "target_handle": "nope"}]},         # no such input
        {"nodes": [{"id": "a", "type": "image"}, {"id": "b", "type": "image"}],
         "edges": [{"source": "a", "target": "b", "target_handle": "refs"},
                   {"source": "b", "target": "a", "target_handle": "refs"}]},          # a loop
        {"nodes": [{"id": "x", "type": "spaceship"}], "edges": []},
    ]
    for graph in bad:
        with pytest.raises(spaces.SpaceError):
            spaces.validate_graph(graph)


def test_templates_are_valid_graphs():
    for name in spaces.TEMPLATES:
        g = spaces.template_graph(name)
        assert isinstance(g["nodes"], list)
    film = spaces.template_graph("reference_film")
    assert {e["target_handle"] for e in film["edges"]} >= {"refs", "start", "prompt"}


def test_a_picture_feeds_a_clip_in_one_run(client, fake_comfy):
    c, _, _ = client
    server, _ = fake_comfy
    pid = c.post("/api/projects", json={"name": "Canvas"}).json()["id"]
    sp = _space(c, pid)
    graph = {"nodes": [
        {"id": "style", "type": "text", "x": 0, "y": 0, "data": {"text": "STYLE: live action, misty forest"}},
        {"id": "pic", "type": "image", "x": 300, "y": 0, "data": {"prompt": "a man offers a dog a steak", "aspect": "16:9", "count": 2}},
        {"id": "clip", "type": "video", "x": 600, "y": 0, "data": {"prompt": "the dog takes the steak", "quality": "draft"}},
    ], "edges": [
        {"id": "e1", "source": "style", "target": "pic", "target_handle": "prompt"},
        {"id": "e2", "source": "pic", "target": "clip", "target_handle": "start"},
    ]}
    saved = c.put(f"/api/spaces/{sp['id']}", json={"graph": graph, "version": sp["version"]}).json()
    assert saved["version"] == sp["version"] + 1
    stale = c.put(f"/api/spaces/{sp['id']}", json={"graph": graph, "version": sp["version"]})
    assert stale.status_code == 409
    run = c.post(f"/api/spaces/{sp['id']}/run", json={"mode": "all"}).json()
    assert run["nodes"] == ["pic", "clip"]
    done = _wait_job(c, run["job"]["id"], 120)
    assert done["state"] == "done", done
    view = c.get(f"/api/spaces/{sp['id']}").json()
    pic, clip = view["state"]["pic"], view["state"]["clip"]
    assert pic["status"] == "done" and len(pic["outputs"]) == 2
    assert clip["status"] == "done" and len(clip["outputs"]) == 2  # one clip per picture
    for aid in clip["outputs"]:
        assert c.get(f"/api/assets/{aid}").json()["kind"] == "video"
    # the style text reached the picture's prompt
    first = c.get(f"/api/assets/{pic['outputs'][0]}").json()
    assert "STYLE: live action" in (first["recipe"].get("prompt") or "")
    # untick one picture: the clip then has one start, and "all" reruns only what changed
    c.patch(f"/api/spaces/{sp['id']}/nodes/pic", json={"excluded": [pic["outputs"][1]]})
    again = c.post(f"/api/spaces/{sp['id']}/run", json={"mode": "all"}).json()
    _wait_job(c, again["job"]["id"], 120)
    after = c.get(f"/api/spaces/{sp['id']}").json()["state"]
    assert after["pic"]["outputs"] == pic["outputs"]  # unchanged: skipped
    assert len(after["clip"]["outputs"]) == 1 and len(after["clip"]["runs"]) == 2


def test_a_missing_input_fails_only_that_node(client):
    c, _, _ = client
    pid = c.post("/api/projects", json={"name": "Canvas 2"}).json()["id"]
    sp = _space(c, pid)
    graph = {"nodes": [{"id": "clip", "type": "video", "data": {"prompt": "x"}},
                       {"id": "song", "type": "music", "data": {"tags": "synth pop, 120 bpm", "duration": 10}}], "edges": []}
    c.put(f"/api/spaces/{sp['id']}", json={"graph": graph})
    run = c.post(f"/api/spaces/{sp['id']}/run", json={"mode": "all"}).json()
    _wait_job(c, run["job"]["id"], 120)
    state = c.get(f"/api/spaces/{sp['id']}").json()["state"]
    assert state["clip"]["status"] == "failed" and "start" in state["clip"]["error"]
    assert state["song"]["status"] == "done"
    assert c.get(f"/api/assets/{state['song']['outputs'][0]}").json()["kind"] == "audio"


def test_cast_and_lists_feed_references(client):
    c, _, _ = client
    pid = c.post("/api/projects", json={"name": "Canvas 3"}).json()["id"]
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (10, 200, 10)).save(buf, "PNG")
    img = c.post(f"/api/projects/{pid}/import-upload", files={"file": ("x.png", buf.getvalue(), "image/png")}).json()["id"]
    char = c.post(f"/api/agent/studio_cast?project={pid}", json={"action": "create", "name": "Snow",
                                                                  "fields": {"prompt": "a white dog", "canonical_asset_id": img}}).json()
    sp = _space(c, pid)
    graph = {"nodes": [{"id": "dog", "type": "cast", "data": {"character_id": char["id"]}},
                       {"id": "pic", "type": "image", "data": {"prompt": "the dog in the snow"}}],
             "edges": [{"id": "e", "source": "dog", "source_handle": "image", "target": "pic", "target_handle": "refs"},
                       {"id": "f", "source": "dog", "source_handle": "text", "target": "pic", "target_handle": "prompt"}]}
    c.put(f"/api/spaces/{sp['id']}", json={"graph": graph})
    space = c.get(f"/api/spaces/{sp['id']}").json()
    from prosperos_hoard.store import Store
    plan = spaces.plan_node(_store(client), space["graph"], space["state"], "pic")
    body = plan[0]["body"]
    assert body["reference_asset_ids"] == [img]
    assert body["prompt"].startswith("@Snow. the dog in the snow") and "<image1>" in body["prompt"]
    assert Store  # imported for the helper below


def _store(client):
    _, app, _ = client
    return app.state.store if hasattr(app.state, "store") else app.state.studio_store


def test_agents_edit_and_run_a_space(client):
    c, _, _ = client
    pid = c.post("/api/projects", json={"name": "Canvas 4"}).json()["id"]
    made = c.post(f"/api/agent/studio_spaces?project={pid}", json={"action": "create", "name": "Agent", "template": "blank"}).json()
    sid = made["id"]
    edited = c.post(f"/api/agent/studio_spaces?project={pid}", json={"action": "edit", "space": sid, "ops": [
        {"op": "add_node", "id": "t", "type": "text", "data": {"text": "a red lantern"}},
        {"op": "add_node", "id": "p", "type": "image", "data": {"aspect": "1:1"}},
        {"op": "connect", "source": "t", "target": "p", "target_handle": "prompt"},
    ]}).json()
    assert [n["id"] for n in edited["graph"]["nodes"]] == ["t", "p"] and len(edited["graph"]["edges"]) == 1
    bad = c.post(f"/api/agent/studio_spaces?project={pid}", json={"action": "edit", "space": sid, "ops": [
        {"op": "connect", "source": "p", "target": "t", "target_handle": "prompt"}]})
    assert bad.status_code == 400
    run = c.post(f"/api/agent/studio_spaces?project={pid}", json={"action": "run", "space": sid, "mode": "node",
                                                                  "node_ids": ["p"]}).json()
    _wait_job(c, run["job"]["id"], 120)
    got = c.post(f"/api/agent/studio_spaces?project={pid}", json={"action": "get", "space": sid}).json()
    assert got["state"]["p"]["status"] == "done" and got["state"]["p"]["outputs"]
    listed = c.post(f"/api/agent/studio_spaces?project={pid}", json={"action": "list"}).json()
    assert listed["items"][0]["cover"] == got["state"]["p"]["outputs"][-1]


def test_prompt_enhancer_keeps_mentions(client):
    c, app, _ = client
    seen = {}

    def chat(messages, max_tokens, temperature):
        seen["user"] = messages[-1]["content"]
        return '"@Snow sitting in fresh snow at dawn, soft rim light, 50mm"'

    app.state.short_hooks = {**(getattr(app.state, "short_hooks", None) or {}), "chat": chat}
    r = c.post("/api/prompt/enhance", json={"text": "@Snow in snow", "kind": "image"}).json()
    assert r["text"] == "@Snow sitting in fresh snow at dawn, soft rim light, 50mm"
    assert "@Snow in snow" in seen["user"]
