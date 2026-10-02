"""Spaces: run to here, the composite (layers) node and techniques that
move a graph (or one group of it) from one project to another."""

from __future__ import annotations

import pytest
from PIL import Image

from prosperos_hoard import spaces
from test_qa import _save
from test_spaces import FakeStudio, _space_with


class LayerStudio(FakeStudio):
    def composite(self, pid, body):
        self.calls.append(("composite", body))
        return {"id": f"a_comp{len(self.calls)}"}


def test_run_to_here_takes_the_nodes_upstream_only():
    g = spaces.validate_graph({"nodes": [
        {"id": "t", "type": "text", "data": {"text": "x"}}, {"id": "a", "type": "image"}, {"id": "b", "type": "image"},
        {"id": "v", "type": "video"}, {"id": "w", "type": "video"}],
        "edges": [{"source": "t", "target": "a", "target_handle": "prompt"},
                  {"source": "a", "target": "v", "target_handle": "start"},
                  {"source": "b", "target": "w", "target_handle": "start"}]})
    assert spaces.run_order(g, "upto", ["v"]) == ["a", "v"]
    assert spaces.run_order(g, "downstream", ["a"]) == ["a", "v"]


def test_a_composite_lays_layers_over_each_background(client):
    store, sid = _space_with(client, {"nodes": [
        {"id": "bg", "type": "asset", "data": {"kind": "image", "asset_ids": []}},
        {"id": "fg", "type": "asset", "data": {"kind": "image", "asset_ids": []}},
        {"id": "mix", "type": "composite", "data": {"layers": [{"blend": "screen", "opacity": 0.7, "scale": 0.5, "x": 0.3}]}},
        {"id": "clip", "type": "video", "data": {"prompt": "slow push in"}}],
        "edges": [{"source": "bg", "target": "mix", "target_handle": "background"},
                  {"source": "fg", "target": "mix", "target_handle": "layers"},
                  {"source": "mix", "target": "clip", "target_handle": "start"}]})
    pid = store.get_space(sid)["project_id"]
    b1, b2 = (_save(store, pid, Image.new("RGB", (64, 36), c)) for c in ((10, 10, 10), (90, 90, 90)))
    fg = _save(store, pid, Image.new("RGB", (20, 20), (255, 255, 255)))
    g = store.get_space(sid)["graph"]
    for n in g["nodes"]:
        if n["id"] == "bg":
            n["data"]["asset_ids"] = [b1, b2]
        if n["id"] == "fg":
            n["data"]["asset_ids"] = [fg]
    store.save_space_graph(sid, g, None, None)
    ops = spaces.plan_node(store, g, {}, "mix")
    assert [o["body"]["background"] for o in ops] == [b1, b2]
    assert ops[0]["body"]["layers"] == [{"asset_id": fg, "blend": "screen", "opacity": 0.7, "scale": 0.5, "x": 0.3}]
    with pytest.raises(spaces.SpaceError):
        spaces.plan_node(store, {**g, "edges": g["edges"][:1]}, {}, "mix")
    studio = LayerStudio()
    spaces.run_space(store, studio, sid, "upto", ["mix"], poll_s=0)
    assert [k for k, _ in studio.calls] == ["composite", "composite"]
    assert spaces.estimate(store, sid)["nodes"][0]["node"] == "mix"


def test_a_real_composite_of_a_picture_and_a_clip(store, project):
    import numpy as np
    from prosperos_hoard import engine
    from test_qa import _clip
    bg = _save(store, project["id"], Image.new("RGB", (128, 72), (20, 40, 80)))
    dot = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    dot.paste((255, 200, 0, 255), (10, 10, 30, 30))
    fg = _save(store, project["id"], dot)
    out = store.get_asset(engine.composite_media(store, project["id"], bg, [{"asset_id": fg, "scale": 0.5, "x": 0.25}])["id"])
    with Image.open(store.data_dir / out["file_path"]) as im:
        px = im.convert("RGB")
        assert px.getpixel((32, 36)) == (255, 200, 0) and px.getpixel((110, 10)) == (20, 40, 80)
    clip = _clip(store, project["id"], np.full((12, 72, 128), 60, dtype=np.uint8))
    mixed = store.get_asset(engine.composite_media(store, project["id"], clip, [{"asset_id": fg, "blend": "screen"}])["id"])
    assert mixed["kind"] == "video" and mixed["recipe"]["operation"] == "composite"
    with pytest.raises(engine.EngineError):
        engine.composite_media(store, project["id"], bg, [{"asset_id": fg, "blend": "dissolve"}])


def test_a_technique_travels_to_another_project(client):
    c, app, _ = client
    store = app.state.store
    src = c.post("/api/projects", json={"name": "Source"}).json()["id"]
    dst = c.post("/api/projects", json={"name": "Target"}).json()["id"]
    for pid in (src, dst):
        c.post(f"/api/agent/studio_cast?project={pid}", json={"action": "create", "name": "Vera", "fields": {"prompt": "a singer"}})
    vera = store.list_characters(src)[0]["id"]
    photo = _save(store, src, Image.new("RGB", (32, 32), (1, 2, 3)))
    graph = {"nodes": [
        {"id": "grp", "type": "group", "x": 0, "y": 0, "w": 900, "h": 500, "data": {"title": "Neon portrait"}},
        {"id": "who", "type": "cast", "x": 40, "y": 60, "data": {"character_id": vera}},
        {"id": "look", "type": "asset", "x": 40, "y": 260, "data": {"kind": "image", "asset_ids": [photo], "app_input": True}},
        {"id": "pic", "type": "image", "x": 400, "y": 60, "data": {"prompt": "neon portrait"}},
        {"id": "far", "type": "video", "x": 2000, "y": 60, "data": {}}],
        "edges": [{"source": "who", "source_handle": "image", "target": "pic", "target_handle": "refs"},
                  {"source": "look", "target": "pic", "target_handle": "refs"},
                  {"source": "pic", "target": "far", "target_handle": "start"}]}
    sp = c.post(f"/api/projects/{src}/spaces", json={"name": "Portraits"}).json()
    c.put(f"/api/spaces/{sp['id']}", json={"graph": graph})
    bundle = c.get(f"/api/spaces/{sp['id']}/export?group=grp").json()
    assert bundle["format"] == spaces.TECHNIQUE_FORMAT and bundle["name"] == "Neon portrait"
    assert {n["id"] for n in bundle["graph"]["nodes"]} == {"grp", "who", "look", "pic"}  # "far" is outside the group
    assert bundle["cast"] == [{"name": "Vera", "kind": "character"}] and "look" in bundle["inputs"]
    assert any("image engine" in m for m in bundle["models"])
    look = next(n for n in bundle["graph"]["nodes"] if n["id"] == "look")
    assert look["data"]["asset_ids"] == [] and "character_id" not in next(n for n in bundle["graph"]["nodes"] if n["id"] == "who")["data"]
    r = c.post(f"/api/projects/{dst}/spaces/import", json={"bundle": bundle})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["cast_bound"] == ["Vera"] and out["to_fill"] == ["look"]
    made = store.get_space(out["space"]["id"])
    who = next(n for n in made["graph"]["nodes"] if n["id"] == "who")
    assert who["data"]["character_id"] == store.list_characters(dst)[0]["id"] and made["name"] == "Neon portrait"
    # added again into the same space: ids stay unique, placed to the right
    again = c.post(f"/api/agent/studio_spaces?project={dst}", json={"action": "import", "bundle": bundle,
                                                                    "space": made["id"]}).json()
    assert "who2" in again["added"]
    g2 = store.get_space(made["id"])["graph"]
    assert min(n["x"] for n in g2["nodes"] if n["id"] == "who2") > max(n["x"] for n in g2["nodes"] if n["id"] == "pic")
    assert c.post(f"/api/projects/{dst}/spaces/import", json={"bundle": {"format": "nope"}}).status_code == 400
