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
