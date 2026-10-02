"""Stock footage (Pexels / Pixabay): search against a mocked HTTP transport,
file choice, import with the credit in the recipe, credits and the API
(keys masked, search + import)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest

from prosperos_hoard import stock
from prosperos_hoard.backend import ffmpeg_path
from prosperos_hoard.hoard_link.web import safety


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Every stock host resolves to a public address, so the tests never depend on the network's DNS."""
    monkeypatch.setattr(safety, "default_resolver", lambda host, port: ["93.184.216.34"])


def make_mp4(path: Path, seconds: float = 3.0, size: str = "360x640") -> bytes:
    subprocess.run([ffmpeg_path(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=24:duration={seconds}",
                    "-pix_fmt", "yuv420p", str(path)], check=True)
    return path.read_bytes()


def pexels_video(i: int, w: int = 1080, h: int = 1920, duration: int = 8) -> dict:
    return {"id": i, "width": w, "height": h, "duration": duration, "url": f"https://www.pexels.com/video/{i}/",
            "image": f"https://images.pexels.com/{i}.jpg", "user": {"name": f"Author {i}", "url": f"https://www.pexels.com/@a{i}"},
            "video_files": [
                {"link": f"https://videos.pexels.com/{i}/uhd.mp4", "file_type": "video/mp4", "width": w * 2, "height": h * 2, "quality": "uhd"},
                {"link": f"https://videos.pexels.com/{i}/hd.mp4", "file_type": "video/mp4", "width": w, "height": h, "quality": "hd"},
                {"link": f"https://videos.pexels.com/{i}/sd.mp4", "file_type": "video/mp4", "width": w // 2, "height": h // 2, "quality": "sd"},
            ]}


def pixabay_video(i: int) -> dict:
    return {"id": i, "pageURL": f"https://pixabay.com/videos/x-{i}/", "duration": 12, "user": f"user{i}", "user_id": 7,
            "tags": "sea, waves, blue",
            "videos": {"large": {"url": f"https://cdn.pixabay.com/{i}/large.mp4", "width": 1080, "height": 1920},
                       "small": {"url": f"https://cdn.pixabay.com/{i}/small.mp4", "width": 540, "height": 960},
                       "tiny": {"url": f"https://cdn.pixabay.com/{i}/tiny.mp4", "width": 360, "height": 640, "thumbnail": "t.jpg"}}}


def transport(mp4: bytes, pexels_status: int = 200, seen: list | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        url = str(request.url)
        if url.startswith("https://api.pexels.com/videos/search"):
            if pexels_status != 200:
                return httpx.Response(pexels_status, json={"error": "nope"})
            assert request.headers["Authorization"] == "PKEY"
            return httpx.Response(200, json={"videos": [pexels_video(1), pexels_video(2, 1920, 1080), pexels_video(3)]})
        if url.startswith("https://pixabay.com/api/videos/"):
            assert request.url.params["key"] == "XKEY"
            return httpx.Response(200, json={"hits": [pixabay_video(10)]})
        if url.endswith(".mp4"):
            return httpx.Response(200, content=mp4, headers={"content-type": "video/mp4"})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_configured_keys_and_status():
    keys = stock.configured_keys({"pexels": " PKEY "}, env={"PIXABAY_API_KEY": "XKEY"})
    assert keys == {"pexels": "PKEY", "pixabay": "XKEY"}
    st = stock.status(keys)
    assert st["providers"]["pexels"] == {"configured": True, "key_hint": "...PKEY", "get_key": "https://www.pexels.com/api/"}
    assert "PKEY" not in json.dumps(st).replace("...PKEY", "")
    assert stock.configured_keys(None, env={}) == {}


def test_search_filters_orientation_and_reports_provider_errors(tmp_path):
    mp4 = make_mp4(tmp_path / "a.mp4")
    seen: list = []
    with httpx.Client(transport=transport(mp4, seen=seen)) as client:
        out = stock.search({"pexels": "PKEY", "pixabay": "XKEY"}, "ocean waves", orientation="portrait", client=client)
    refs = [it["ref"] for it in out["items"]]
    assert refs == ["pexels:1", "pexels:3", "pixabay:10"]  # the landscape one is dropped
    assert out["items"][0]["author"] == "Author 1" and out["items"][2]["tags"] == ["sea", "waves", "blue"]
    assert seen[0].url.params["orientation"] == "portrait"
    with httpx.Client(transport=transport(mp4, pexels_status=401)) as client:
        out = stock.search({"pexels": "PKEY", "pixabay": "XKEY"}, "ocean", client=client, exclude={"pixabay:99"})
    assert [it["ref"] for it in out["items"]] == ["pixabay:10"] and "rejected" in out["errors"]["pexels"]
    with pytest.raises(stock.StockError, match="no stock provider"):
        stock.search({}, "ocean")
    with pytest.raises(stock.StockError, match="query"):
        stock.search({"pexels": "k"}, "  ")


def test_pick_file_prefers_the_smallest_big_enough():
    item = {"ref": "pexels:1", "files": pexels_video(1)["video_files"]}
    item["files"] = [{"url": f["link"], "width": f["width"], "height": f["height"]} for f in item["files"]]
    assert stock.pick_file(item, 1080)["url"].endswith("/hd.mp4")
    assert stock.pick_file(item, 540)["url"].endswith("/sd.mp4")
    assert stock.pick_file(item, 5000)["url"].endswith("/uhd.mp4")


def test_fetch_imports_with_credit(store, project, tmp_path):
    mp4 = make_mp4(tmp_path / "a.mp4")
    with httpx.Client(transport=transport(mp4)) as client:
        item = stock.search({"pexels": "PKEY"}, "ocean", client=client)["items"][0]
        asset = stock.fetch(store, project["id"], item, short_side=1080, query="ocean", client=client)
    assert asset["kind"] == "video" and asset["duration_s"] and asset["source"] == "import"
    r = asset["recipe"]
    assert r["operation"] == "stock" and r["provider"] == "pexels" and r["author"] == "Author 1"
    assert r["page_url"] == "https://www.pexels.com/video/1/" and "Pexels License" in r["license"]
    assert "stock" in asset["tags"] and asset["name"] == "Pexels 1 - Author 1"
    assert stock.credits([asset, asset]) == ["Video by Author 1 on Pexels - https://www.pexels.com/video/1/"]


def _item(url: str) -> dict:
    return {"provider": "pexels", "id": "9", "kind": "video", "ref": "pexels:9", "files": [{"url": url, "width": 1080, "height": 1920}]}


def test_a_stock_file_on_a_private_address_is_never_fetched(store, project):
    asked: list = []
    client = httpx.Client(transport=httpx.MockTransport(lambda request: asked.append(request) or httpx.Response(200, content=b"x")))
    for bad in ("http://169.254.169.254/latest/meta-data", "http://127.0.0.1:8188/view", "http://10.0.0.5/a.mp4", "file:///etc/passwd"):
        with pytest.raises(stock.StockError) as caught:
            stock.fetch(store, project["id"], _item(bad), client=client)
        assert caught.value.code == "stock_download_blocked", bad
    assert asked == []


def test_a_redirect_to_a_private_address_is_stopped_and_a_public_one_followed(store, project, tmp_path):
    mp4 = make_mp4(tmp_path / "a.mp4")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "videos.pexels.com" and request.url.path == "/evil.mp4":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})
        if request.url.host == "videos.pexels.com" and request.url.path == "/moved.mp4":
            return httpx.Response(301, headers={"location": "https://cdn.pexels.com/real.mp4"})
        if request.url.host == "cdn.pexels.com":
            return httpx.Response(200, content=mp4, headers={"content-type": "video/mp4"})
        raise AssertionError(f"reached {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(stock.StockError) as caught:
            stock.fetch(store, project["id"], _item("https://videos.pexels.com/evil.mp4"), client=client)
        assert caught.value.code == "stock_download_blocked"
        asset = stock.fetch(store, project["id"], _item("https://videos.pexels.com/moved.mp4"), client=client)
    assert asset["kind"] == "video"


def test_a_huge_stock_file_is_refused_before_downloading_it(store, project, monkeypatch):
    sent = {"bytes": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-length": str(stock.MAX_DOWNLOAD_BYTES + 1)}, content=b"x" * 10)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(stock.StockError) as caught:
            stock.fetch(store, project["id"], _item("https://videos.pexels.com/big.mp4"), client=client)
    assert caught.value.code == "too_large"
    monkeypatch.setattr(stock, "MAX_DOWNLOAD_BYTES", 5)                      # and one that lies about its length is cut off
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 50))) as client:
        with pytest.raises(stock.StockError) as caught:
            stock.fetch(store, project["id"], _item("https://videos.pexels.com/lie.mp4"), client=client)
    assert caught.value.code == "too_large"


def test_stock_api_keys_and_search_import(client, tmp_path):
    c, app, _ = client
    r = c.put("/api/backend/stock", json={"pexels": "PKEY"})
    assert r.status_code == 200 and r.json()["providers"]["pexels"]["configured"] is True
    assert c.get("/api/backend/stock").json()["providers"]["pixabay"]["configured"] is False
    assert c.put("/api/backend/stock", json={"pexels": "has space"}).status_code == 400
    mp4 = make_mp4(tmp_path / "b.mp4")
    app.state.short_hooks = {"stock_client": lambda: httpx.Client(transport=transport(mp4))}
    project = c.post("/api/projects", json={"name": "Stock"}).json()
    r = c.post("/api/agent/studio_stock_search", json={"query": "ocean", "aspect": "9:16", "project": project["id"], "take": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [i["ref"] for i in body["items"]] == ["pexels:1", "pexels:3"]
    assert len(body["imported"]) == 1 and body["imported"][0]["kind"] == "video"
    r = c.post("/api/agent/studio_stock_search", json={"query": "ocean", "project": project["id"], "refs": ["pexels:3"]})
    assert r.status_code == 200 and len(r.json()["imported"]) == 1
    r = c.post("/api/agent/studio_stock_search", json={"query": "ocean", "project": project["id"], "refs": ["pexels:77"]})
    assert r.status_code == 400 and "unknown_ref" in r.text
    c.put("/api/backend/stock", json={"pexels": ""})
    assert c.get("/api/backend/stock").json()["providers"]["pexels"]["configured"] is False


def test_the_app_own_client_connects_to_the_address_that_was_checked(tmp_path, monkeypatch):
    connected: list = []
    monkeypatch.setattr(safety, "pinned_transport",
                        lambda ip, **kw: connected.append(ip) or httpx.MockTransport(lambda r: httpx.Response(200, content=b"data")))
    dest = tmp_path / "out.bin"
    stock._download(httpx.Client(), "https://videos.pexels.com/a.mp4", dest, pin=True)
    assert connected == ["93.184.216.34"] and dest.read_bytes() == b"data"
    monkeypatch.setattr(safety, "default_resolver", lambda host, port: ["10.1.2.3"])   # a name that points inside is refused
    with pytest.raises(stock.StockError) as caught:
        stock._download(httpx.Client(), "https://videos.pexels.com/a.mp4", dest, pin=True)
    assert caught.value.code == "stock_download_blocked"
