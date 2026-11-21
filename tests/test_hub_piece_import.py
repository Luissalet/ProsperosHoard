"""Reproducible imports of order media from the Hub."""

import hashlib
import io

import httpx
from PIL import Image
import pytest

from prosperos_hoard import engine, hub_piece


def test_import_piece_uses_its_family_token_and_records_the_received_bytes(store, project, monkeypatch):
    png = io.BytesIO()
    Image.new("RGB", (32, 32)).save(png, format="PNG")
    blob = png.getvalue()
    (store.data_dir / "mcp-token").write_text("prospero-family-token", encoding="utf-8")
    monkeypatch.setenv("HOARD_HUB_URL", "http://127.0.0.1:65501")
    observed = []

    def respond(request):
        observed.append(request)
        status = 200 if request.url.path.endswith("/17") else 409
        return httpx.Response(status, content=blob, headers={"content-type": "image/png"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(hub_piece.httpx, "stream",
                            lambda method, url, **kw: http.stream(method, url, params=kw["params"], headers=kw["headers"]))
        asset = hub_piece.import_piece(store, project["id"], 17, preview=0)
        with pytest.raises(engine.EngineError) as exc:
            hub_piece.import_piece(store, project["id"], 18)
    assert exc.value.code == "hub_piece_unavailable"
    request = observed[0]
    assert str(request.url) == "http://127.0.0.1:65501/api/agora/piece_file/17?preview=0"
    assert request.headers["authorization"] == "Bearer prospero-family-token"
    assert asset["recipe"] == {"hub_piece": {"id": 17, "preview": 0, "sha256": hashlib.sha256(blob).hexdigest()}}


def test_register_asset_as_piece_and_undo(store, project, monkeypatch, tmp_path):
    source = tmp_path / "guide.png"
    Image.new("RGB", (32, 32)).save(source)
    asset = engine.import_asset(store, project["id"], source, "image",
                                recipe={"template": "qwen21_depth"})
    (store.data_dir / "mcp-token").write_text("prospero-family-token", encoding="utf-8")
    calls = []

    def fetch(url, args, *, headers, timeout):
        calls.append((url, args))
        assert headers["Authorization"] == "Bearer prospero-family-token"
        if url.endswith("/piece_add"):
            return 200, {"ok": True, "piece": {"id": 17, "sha256": "hash"}, "undo": "receipt"}
        return 200, {"ok": True, "undone": "piece_add"}

    monkeypatch.setattr(hub_piece, "fetch", fetch)
    registered = hub_piece.register_piece(store, asset["id"], space="estudio", order_id=4,
                                         title="Depth key", derives_from=[10], agent="artist")
    assert registered["piece_id"] == 17 and registered["sha256"] == "hash"
    assert calls[0][1] == {"space_id": "estudio", "agent": "artist", "order_id": 4, "task_id": None,
                           "title": "Depth key", "kind": "image", "path": str((store.data_dir / asset["file_path"]).resolve()),
                           "app": "prospero", "object": asset["id"], "place": project["id"],
                           "model": "qwen21_depth", "derives_from": [10]}
    assert hub_piece.undo_registration(store, registered["undo"]["arguments"])["undone"] == "piece_add"
    assert calls[1][1] == {"space_id": "estudio", "agent": "artist", "token": "receipt"}


def test_register_take_and_frame_preserve_order_materials(store, project, monkeypatch, tmp_path):
    png = tmp_path / "guide.png"
    Image.new("RGB", (32, 32)).save(png)
    digest = hashlib.sha256(png.read_bytes()).hexdigest()
    pid = project["id"]
    guide = engine.import_asset(store, pid, png, "image", recipe={"hub_piece": {"id": 21, "sha256": digest}})
    external = engine.import_asset(store, pid, png, "image", recipe={"hub_piece": {"id": 99, "sha256": digest}})
    frame = engine.import_asset(store, pid, png, "image", recipe={"operation": "frame", "derived_from": "take"})
    take = {"id": "take", "kind": "video", "file_path": frame["file_path"], "project_id": pid,
            "recipe": {"input_asset_ids": [guide["id"], external["id"], "deleted"]}}
    retake = {**take, "id": "retake", "recipe": {"retake_of": "take"}}
    media = {asset["id"]: asset for asset in (take, retake)}
    get_asset = store.get_asset
    monkeypatch.setattr(store, "get_asset", lambda aid: media[aid] if aid in media else get_asset(aid))
    (store.data_dir / "mcp-token").write_text("prospero-family-token", encoding="utf-8")
    pieces = [{"id": 21, "app": "vulcan", "object": "render", "status": "checked", "sha256": digest},
              {"id": 99, "app": "vulcan", "object": "different", "status": "checked", "sha256": "other-digest"}]
    writes = []

    def fetch(url, args=None, *, headers, timeout):
        if url.endswith("order?space_id=estudio&order_id=4"):
            return 200, {"ok": True, "order": {"piece_list": pieces}}
        assert url.endswith("/piece_add")
        writes.append(args)
        piece = {"id": 30 + len(writes), "app": "prospero", "object": args["object"],
                 "status": "provisional", "sha256": "hash"}
        pieces.append(piece)
        return 200, {"ok": True, "piece": piece, "undo": "receipt"}

    monkeypatch.setattr(hub_piece, "fetch", fetch)
    args = {"space": "estudio", "order_id": 4, "agent": "artist"}
    hub_piece.register_piece(store, "take", title="Take", **args)
    hub_piece.register_piece(store, frame["id"], title="Frame", **args)
    hub_piece.register_piece(store, "retake", title="Retake", **args)
    hub_piece.register_piece(store, "take", title="Reference only",
                             derives_from=[{"piece": 21, "role": "reference"}], **args)
    assert [write["derives_from"] for write in writes] == [
        [{"piece": 21, "role": "material"}], [{"piece": 31, "role": "material"}],
        [{"piece": 31, "role": "material"}],
        [{"piece": 21, "role": "reference"}],
    ]
