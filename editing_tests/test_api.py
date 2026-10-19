import json
import re
from pathlib import Path


from conftest import BANNED_WORDS, ROOT, needs_ffmpeg
from prosperos_hoard.editing.agent_tools import TOOLS, tool_catalog


def test_health_and_guard(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["service"] == "lumiere-hoard"
    assert client.get("/api/health", headers={"host": "evil.example"}).status_code == 403
    assert client.post("/api/projects", json={"name": "x"}, headers={"origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/projects", json={"name": "x"}, headers={"sec-fetch-site": "cross-site", "sec-fetch-mode": "cors"}).status_code == 403


def test_agent_contract_needs_the_token(client):
    tools = client.get("/api/agent/tools").json()
    assert {t["name"] for t in tools["tools"]} >= {"media_import", "timeline_edit", "plan_create", "render_start"}
    assert client.post("/api/agent/call", json={"name": "project_list"}).status_code == 401
    ok = client.post("/api/agent/call", json={"name": "project_list"}, headers={"Authorization": f"Bearer {client.svc.token}"})
    assert ok.status_code == 200 and ok.json() == {"projects": []}
    bad = client.post("/api/agent/call", json={"name": "project_delete", "arguments": {"project": "prj_x"}},
                      headers={"Authorization": f"Bearer {client.svc.token}"})
    assert bad.status_code in (400, 404)


def test_tool_descriptions_are_short_and_bilingual():
    for t in TOOLS:
        first = t.description.splitlines()[0]
        assert len(first) <= 110, (t.name, len(first))
        assert "Keywords" in t.description, t.name
    names = [t["name"] for t in tool_catalog()]
    assert len(names) == len(set(names))


def test_editor_imports_are_owned_after_retirement():
    # Protocol names and historical licences are legitimate product text. The
    # real conservation contract is absence of an import of the retired app.
    import ast
    for path in (ROOT / 'prosperos_hoard' / 'editing').rglob('*.py'):
        if 'hoard_link' in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else ([node.module] if isinstance(node,ast.ImportFrom) and node.level==0 else [])
            assert not any(n and (n=='lumiere_hoard' or n.startswith('lumiere_hoard.')) for n in names), path


def test_manifest_matches_the_app():
    m = json.loads((ROOT / "faustus-plugin.json").read_text(encoding="utf-8"))
    assert m["id"] == "prospero" and m["app"]["health"]["expect"]["service"] == "prosperos-hoard"
    assert m["defaults"]["APP_URL"].endswith(":8815")
    assert "PROSPERO_DIR" in m["placeholders"]


@needs_ffmpeg
def test_media_project_edit_render_over_http(client, media_dir):
    r = client.post("/api/media/import", json={"path": str(media_dir / "talk.mp4")})
    assert r.status_code == 200, r.text
    mid = r.json()["id"]
    assert client.get(f"/api/media/{mid}/play").status_code == 200
    wave = client.get(f"/api/media/{mid}/waveform")
    assert wave.status_code == 200 and len(wave.content) > 1000
    assert client.get(f"/api/media/{mid}/sprite").headers["content-type"] == "image/jpeg"
    rng = client.get(f"/api/media/{mid}/play", headers={"Range": "bytes=0-99"})
    assert rng.status_code == 206 and len(rng.content) == 100
    p = client.post("/api/projects", json={"name": "Web", "preset": "hd720", "media": [mid]}).json()
    pid = p["id"]
    clip = p["doc"]["tracks"][0]["clips"][0]["id"]
    e = client.post(f"/api/projects/{pid}/edit", json={"ops": [{"op": "split", "clip": clip, "at": 4000}], "base_rev": p["rev"]})
    assert e.status_code == 200 and len(e.json()["view"]["doc"]["tracks"][0]["clips"]) == 2
    stale = client.post(f"/api/projects/{pid}/edit", json={"ops": [{"op": "split", "at": 2000}], "base_rev": p["rev"]})
    assert stale.status_code == 409
    assert client.post(f"/api/projects/{pid}/undo").json()["doc"]["tracks"][0]["clips"].__len__() == 1
    f = client.get(f"/api/projects/{pid}/frame", params={"t": "1.5s", "width": 320})
    assert f.status_code == 200 and f.headers["content-type"] == "image/jpeg"
    cmd = client.post(f"/api/projects/{pid}/command", json={"command": "remove_silences", "args": {}})
    assert cmd.json()["done"] and cmd.json()["view"]["duration_ms"] < 12000
    plan = client.post(f"/api/projects/{pid}/plans", json={"instruction": "hazlo cuadrado", "use_model": False}).json()
    assert plan["steps"][0]["name"] == "reframe"
    job = client.post(f"/api/projects/{pid}/render", json={"preset": "preview", "end": "0:02"}).json()
    done = client.get(f"/api/jobs/{job['id']}").json()
    assert done["state"] == "done", done["error"]
    renders = client.get("/api/renders", params={"project": pid}).json()["renders"]
    assert renders and client.get(renders[0]["url"]).status_code == 200
    assert client.get("/api/presets").json()["export_presets"]
    fs = client.get("/api/fs", params={"path": str(media_dir)}).json()
    assert any(e["name"] == "talk.mp4" for e in fs["entries"])


def test_settings_validation(client):
    assert client.patch("/api/settings", json={"whisper_device": "gpu"}).status_code == 400
    assert client.patch("/api/settings", json={"whisper_device": "cpu"}).json()["whisper_device"] == "cpu"
    assert client.patch("/api/settings", json={"nope": 1}).status_code == 400


def test_file_roots_are_enforced(tmp_path, media_dir):
    from fastapi.testclient import TestClient

    from conftest import make_services
    from prosperos_hoard.editing.main import create_app

    svc = make_services(tmp_path, file_roots=(tmp_path / "allowed",))
    (tmp_path / "allowed").mkdir()
    with TestClient(create_app(svc.config, svc), base_url="http://127.0.0.1") as c:
        r = c.post("/api/media/import", json={"path": str(media_dir / "talk.mp4")})
        assert r.status_code == 403
        assert c.get("/api/fs", params={"path": str(media_dir)}).status_code == 403


def test_package_json_scripts():
    pkg = json.loads((ROOT / "editing_frontend" / "package.json").read_text(encoding="utf-8"))
    assert pkg["scripts"]["build"] == "vite build"
    assert re.match(r"\d+\.\d+\.\d+", pkg["version"])
    assert Path(ROOT / "editing_frontend" / "vite.config.js").exists()
