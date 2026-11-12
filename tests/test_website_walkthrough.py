from __future__ import annotations

import functools
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from prosperos_hoard import engine, website_walkthrough as walk
from prosperos_hoard.store import Store


@pytest.mark.parametrize("steps", [[{"action": "fill"}], [{"action": "click"}], [{"action": "scroll", "y": "bad"}]])
def test_bad_steps_fail_before_recording(steps):
    with pytest.raises(engine.EngineError):
        walk.plan("http://127.0.0.1:1234", steps)


def test_local_urls_and_relative_navigation_are_supported():
    spec = walk.plan("http://127.0.0.1:1234/app", [{"action": "navigate", "url": "/result", "caption": "Resultado"}])
    assert spec["steps"][0]["url"] == "http://127.0.0.1:1234/result"
    with pytest.raises(engine.EngineError):
        walk.plan("file:///tmp/index.html", [])


def test_api_catalogue_and_job_validation(client):
    c, app, _ = client
    tools = c.get("/api/agent/tools").json()
    assert "website_walkthrough" in str(tools)
    response = c.post("/api/agent/website_walkthrough", json={"url": "file:///tmp/x"})
    assert response.status_code == 400
    assert not app.state.store.list_projects()["items"]


def test_real_browser_records_click_navigation_captions_and_imports_assets(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text('<title>Inicio</title><h1>Entrega</h1><a href="result.html">Ver resultado</a>', encoding="utf-8")
    (site / "result.html").write_text('<title>Resultado</title><h1>Encargo terminado</h1>', encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(SimpleHTTPRequestHandler, directory=str(site)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        store = Store(tmp_path / "data")
        project = store.create_project("Delivery")
        result = walk.render_job(store, {"params": {"project_id": project["id"], "url": f"http://127.0.0.1:{server.server_port}/",
            "title": "Vista inicial", "hold_s": 0.5, "steps": [{"action": "click", "selector": "text=Ver resultado", "caption": "Resultado real"}]}}, lambda *a: None)
        assert result["capture"] == "browser-video"
        assert result["file"]["frames"] > 10
        assert result["file"]["codec"] == "h264"
        assert result["steps"][0]["page_title"] == "Resultado"
        asset = store.get_asset(result["asset_id"])
        assert asset["kind"] == "video"
        assert asset["recipe"]["steps"][0]["caption"] == "Resultado real"
        assert store.get_asset(result["contact_sheet_id"])["kind"] == "image"
        assert not list((store.data_dir / "tmp").glob("walkthrough-*"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
