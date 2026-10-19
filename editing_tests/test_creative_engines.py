from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
from PIL import Image
from PIL import ImageChops

from prosperos_hoard.editing.creative_engines import CreativeEngineError, CreativeEngines


def test_incorporated_discovery_ignores_external_configuration(tmp_path, monkeypatch):
    import prosperos_hoard.editing.creative_engines as module
    product=tmp_path/"product"
    monkeypatch.setattr(module,"__file__",str(product/"prosperos_hoard/editing/creative_engines.py"))
    binary=product/"native/editing/bin"/("lumiere-compositor.exe" if os.name=="nt" else "lumiere-compositor")
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"owned engine fixture")
    external=tmp_path/"external.exe";external.write_bytes(b"external")
    monkeypatch.setenv("LUMIERE_EFFECTCRAFT_CLI",str(external))
    monkeypatch.setenv("LUMIERE_FILMCRAFT_CLI",str(external))
    engines=CreativeEngines(tmp_path/"data")
    engines.config_path.write_text(json.dumps({"effectcraft":str(external),"filmcraft":str(external)}),encoding="utf-8")
    assert engines.executable("motion")==binary
    assert engines.executable("edit") is None
    assert engines.status()["engines"]["motion"]["incorporated"]
    with pytest.raises(CreativeEngineError):
        engines.executable("effectcraft")
    folder=engines._project_dir("a"*32)
    engines._guard_paths("motion",[{"arguments":{"path":str(folder/"frame.png")}}],folder)
    with pytest.raises(CreativeEngineError,match="inside this creative project folder"):
        engines._guard_paths("edit",[{"arguments":{"path":str(external)}}],folder)


def test_saved_catalogue_keeps_historical_files_and_reports_partial_errors(tmp_path):
    engines=CreativeEngines(tmp_path/"data")
    cid="a"*32
    folder=engines._project_dir(cid);folder.mkdir()
    original=folder/(cid+".ecproj");original.write_text('{"original":"unchanged"}',encoding="utf-8")
    manifest={"id":cid,"engine":"effectcraft","native_path":str(original),"title":"Stored title",
              "project_id":"prj_12345678","state":"complete","created_at":"2026-10-13T08:00:00Z"}
    engines._save_manifest(manifest)
    broken=engines.projects_dir/("b"*32);broken.mkdir();(broken/"manifest.json").write_text("bad",encoding="utf-8")
    before=original.read_bytes()
    listed=engines.list("motion","prj_12345678")
    assert listed["count"]==1 and listed["items"][0]["historical_import"]
    assert len(listed["errors"])==1 and original.read_bytes()==before
    assert not original.with_suffix(".lumotion").exists()
    assert engines.list("edit")["count"]==0


def test_catalogue_and_get_are_available_through_native_api_and_agent(client):
    svc=client.app.state.services
    engines=CreativeEngines(svc.config.data_dir)
    cid="c"*32
    folder=engines._project_dir(cid);folder.mkdir()
    native=folder/(cid+".lumotion");native.write_text('{}',encoding="utf-8")
    engines._save_manifest({"id":cid,"engine":"motion","native_path":str(native),"title":"Persisted example",
                           "state":"complete","created_at":"2026-10-13T08:00:00Z","preview_path":None,"render_path":None})
    assert client.get('/api/creative').json()['items'][0]['id']==cid
    assert client.get(f'/api/creative/{cid}').json()['project_url'].endswith(f'/{cid}/project')
    from prosperos_hoard.editing.agent_tools import CreativeListArgs, CreativeRenderArgs, run_creative_list, run_creative_get
    assert run_creative_list(svc,CreativeListArgs())['count']==1
    assert run_creative_get(svc,CreativeRenderArgs(creative_id=cid))['title']=='Persisted example'


def _configured() -> CreativeEngines:
    data = os.environ.get("LUMIERE_CREATIVE_TEST_DATA", "")
    if not data:
        pytest.skip("set LUMIERE_CREATIVE_TEST_DATA to an isolated directory for real engine tests")
    engines = CreativeEngines(Path(data))
    if not engines.executable("motion") or not engines.executable("edit"):
        pytest.skip("build both incorporated native engines for real engine tests")
    return engines


@pytest.mark.skipif(not os.environ.get("LUMIERE_CREATIVE_TEST_DATA"), reason="opt-in real incorporated engines")
def test_historical_projects_open_as_own_copies_with_original_bytes_preserved(tmp_path):
    engines = _configured()
    title = engines.create_title_card(text="Preserved legacy project",width=320,height=180,fps=10,duration=1)
    folder=Path(title["project_dir"])
    original=folder/(title["id"]+".ecproj")
    original.write_bytes(Path(title["native_path"]).read_bytes())
    digest=hashlib.sha256(original.read_bytes()).hexdigest()
    Path(title["native_path"]).unlink()
    title.update(engine="effectcraft",native_path=str(original))
    engines._save_manifest(title)
    migrated=engines.get(title["id"])
    assert migrated["engine"]=="motion" and Path(migrated["native_path"]).suffix==".lumotion"
    assert migrated["id"]==title["id"] and hashlib.sha256(original.read_bytes()).hexdigest()==digest
    answer=engines.call(title["id"],[{"tool":"get_comp","arguments":{}}])
    assert answer["ok"] and hashlib.sha256(original.read_bytes()).hexdigest()==digest


@pytest.mark.skipif(not os.environ.get("LUMIERE_CREATIVE_TEST_DATA"), reason="opt-in real incorporated engines")
def test_motion_export_keeps_native_source_and_other_queue_jobs_unchanged():
    engines = _configured()
    title = engines.create_title_card(text="Queue and source",width=320,height=180,fps=10,duration=1)
    source=Path(title["native_path"])
    before=hashlib.sha256(source.read_bytes()).hexdigest()
    first=engines.render_title_video(title["id"])
    assert first["render_frame_count"]==10 and hashlib.sha256(source.read_bytes()).hexdigest()==before
    second=engines.render_title_video(title["id"])
    assert second["render_frame_count"]==10 and hashlib.sha256(source.read_bytes()).hexdigest()==before


@pytest.mark.skipif(not os.environ.get("LUMIERE_CREATIVE_TEST_DATA"), reason="opt-in real incorporated engines")
def test_export_snapshot_keeps_concurrent_edit_and_reports_older_artifacts(monkeypatch):
    engines = _configured()
    title=engines.create_title_card(text="Snapshot source",width=320,height=180,fps=10,duration=1)
    source=Path(title["native_path"])
    before=source.read_bytes()
    original_request=engines._request
    changed=False
    async def edit_after_snapshot(engine,folder,calls,**kwargs):
        nonlocal changed
        result=await original_request(engine,folder,calls,**kwargs)
        if not changed and kwargs.get('native_path') and calls[0]['tool']=='get_comp':
            changed=True
            edited=await original_request(engine,folder,[{'tool':'set_property','arguments':{
                'layer':title['text_layer_id'],'path':'transform/position','value':[40,60]}}],open_saved=True)
            assert not any(call['is_error'] for call in edited)
            current=engines.get(title['id']);current['last_tools']=['concurrent_edit']
            engines._save_manifest(current)
        return result
    monkeypatch.setattr(engines,'_request',edit_after_snapshot)
    rendered=engines.render_title_video(title['id'])
    assert changed and source.read_bytes()!=before
    assert Path(rendered['render_source_path']).read_bytes()==before
    assert rendered['render_source_sha256']==hashlib.sha256(before).hexdigest()
    assert rendered['render_source_matches'] is False and rendered['preview_source_matches'] is False
    assert engines.get(title['id'])['last_tools']==['concurrent_edit']
    assert rendered['render_frame_count']==10


@pytest.mark.skipif(not os.environ.get("LUMIERE_CREATIVE_TEST_DATA"), reason="opt-in real incorporated engines")
def test_overlapping_call_keeps_completed_export_and_saves_under_lock(monkeypatch):
    import prosperos_hoard.editing.creative_engines as module
    engines = _configured()
    title = engines.create_title_card(text="Concurrent publication", width=160, height=90, fps=12, duration=1)
    ready, resume = threading.Event(), threading.Event()
    original_guard, original_save = engines._guard_paths, engines._save_manifest
    result = {}

    def guard(engine, calls, folder):
        original_guard(engine, calls, folder)
        if threading.current_thread().name == "native-edit":
            ready.set()
            assert resume.wait(30)

    def save(manifest):
        if threading.current_thread().name == "native-edit":
            acquired = []
            def try_lock():
                locked = module._LOCK.acquire(blocking=False)
                acquired.append(locked)
                if locked:
                    module._LOCK.release()
            probe = threading.Thread(target=try_lock)
            probe.start(); probe.join(5)
            assert acquired == [False], "call publication must hold the shared lock"
        return original_save(manifest)

    monkeypatch.setattr(engines, "_guard_paths", guard)
    monkeypatch.setattr(engines, "_save_manifest", save)
    def edit():
        try:
            result["call"] = engines.call(title["id"], [{"tool": "set_property", "arguments": {
                "layer": title["text_layer_id"], "path": "transform/position", "value": [50, 40]}}])
        except BaseException as error:
            result["error"] = error
    worker = threading.Thread(target=edit, name="native-edit")
    worker.start()
    try:
        assert ready.wait(30)
        rendered = engines.render_title_video(title["id"])
    finally:
        resume.set(); worker.join(30)
    assert not worker.is_alive() and "error" not in result
    assert result["call"]["ok"]
    current = engines.get(title["id"])
    for key in ("render_path", "render_state", "render_source_sha256", "preview_path", "preview_source_sha256"):
        assert current[key] == rendered[key]
    assert current["render_frame_count"] == 12 and Path(current["render_path"]).is_file()
    assert current["last_tools"] == ["set_property"]
    assert current["render_source_matches"] is False and current["preview_source_matches"] is False


@pytest.mark.skipif(not os.environ.get("LUMIERE_CREATIVE_TEST_DATA"), reason="opt-in real executable integration")
def test_motion_title_card_is_editable_keyframed_and_rendered():
    engines = _configured()
    result = engines.create_title_card(text="Lumiere title", width=320, height=180, fps=24, duration=1.5)
    project = Path(result["native_path"])
    preview = Path(result["preview_path"])
    assert project.is_file() and project.stat().st_size > 100
    with Image.open(preview) as image:
        assert image.size == (320, 180)
        rgb = image.convert("RGB")
        assert len(rgb.getcolors(maxcolors=320 * 180) or []) > 2
    opacity = next(c for c in result["calls"] if c["tool"] == "get_property")
    prop = json.loads(opacity["content"][0]["text"])
    assert len(prop.get("keys", [])) == 4 and prop.get("animated") is True
    saved = engines.get(result["id"])
    assert saved["state"] == "complete" and saved["editable"]
    catalogue = engines.tools("motion")
    assert catalogue["count"] == 21
    proxy = engines.call(result["id"], [{"tool": "get_project", "arguments": {}}, {"tool": "get_comp", "arguments": {}}])
    assert proxy["ok"] and [call["tool"] for call in proxy["calls"] if call["tool"] not in {"open_project", "save_project"}] == ["get_project", "get_comp"]

    rendered = engines.render_title_video(result["id"])
    mp4 = Path(rendered["render_path"])
    assert mp4.is_file() and mp4.stat().st_size > 1000
    assert rendered["render_state"] == "complete" and rendered["render_codec"] == "h264"
    assert rendered["render_fps"] == pytest.approx(24)
    assert rendered["render_duration_seconds"] == pytest.approx(1.5, abs=1 / 24)
    assert rendered["render_frame_count"] == 36
    assert rendered["media_receive_url"].endswith(f"/api/creative/{result['id']}/render")
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg, "ffmpeg is required to verify distinct frames in the opt-in real render test"
    start_frame, middle_frame = mp4.parent / "qa-start.png", mp4.parent / "qa-middle.png"
    for at, destination in ((0.05, start_frame), (0.75, middle_frame)):
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", str(at), "-i", str(mp4),
                        "-frames:v", "1", str(destination)], check=True, timeout=30)
    with Image.open(start_frame).convert("RGB") as first, Image.open(middle_frame).convert("RGB") as middle:
        difference = ImageChops.difference(first, middle)
        assert difference.getbbox() is not None, "title opacity keyframes should produce visibly different frames"


@pytest.mark.skipif(not os.environ.get("LUMIERE_CREATIVE_TEST_DATA"), reason="opt-in real executable integration")
def test_edit_imports_copy_saves_project_and_exports_video(tmp_path):
    engines = _configured()
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("ffmpeg and ffprobe are required to generate and verify the isolated source")
    source = tmp_path / "synthetic-source.mp4"
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=0x366a9a:s=160x90:r=24:d=1.25", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", str(source)], check=True, timeout=30)
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    result = engines.create_film_sequence(source, source_media_id="med_test1234", width=160, height=90, fps=24)
    project = Path(result["native_path"])
    render = Path(result["render_path"])
    assert project.is_file() and project.stat().st_size > 100
    assert render.is_file() and render.stat().st_size > 1000
    assert Path(result["source_copy"]).is_file()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original
    probe = subprocess.run([ffprobe, "-v", "error", "-show_entries", "stream=codec_type,width,height", "-of", "json", str(render)],
                           check=True, capture_output=True, text=True, timeout=30)
    streams = json.loads(probe.stdout)["streams"]
    assert any(s.get("codec_type") == "video" and s.get("width") == 160 and s.get("height") == 90 for s in streams)
    sequence = next(c for c in result["calls"] if c["tool"] == "sequence_inspect")
    timeline = json.loads(sequence["content"][0]["text"])
    assert any(track.get("items") for track in timeline.get("video", []))
    catalogue = engines.tools("edit")
    assert catalogue["count"] == 9
    proxy = engines.call(result["id"], [{"tool": "project_inspect", "arguments": {}}, {"tool": "sequence_inspect", "arguments": {}}])
    assert proxy["ok"] and any(call["tool"] == "command_run:file.open" for call in proxy["calls"])
    # Preserve an older imported editing project as a historical source, while
    # all subsequent operations run against the own format and engine.
    historical=project.with_suffix(".fcproj")
    old=json.loads(project.read_text(encoding="utf-8"))
    old["format"]="filmcraft.project"
    historical.write_text(json.dumps(old),encoding="utf-8")
    legacy_sha=hashlib.sha256(historical.read_bytes()).hexdigest()
    project.unlink()
    manifest=engines.get(result["id"])
    manifest.update(engine="filmcraft",native_path=str(historical))
    engines._save_manifest(manifest)
    migrated=engines.get(result["id"])
    assert migrated["engine"]=="edit" and Path(migrated["native_path"]).suffix==".lumedit"
    assert json.loads(Path(migrated["native_path"]).read_text(encoding="utf-8"))["format"]=="lumiere-edit.project"
    assert engines.call(result["id"],[{"tool":"sequence_inspect","arguments":{}}])["ok"]
    assert hashlib.sha256(historical.read_bytes()).hexdigest()==legacy_sha
    assert hashlib.sha256(source.read_bytes()).hexdigest()==original


@pytest.mark.skipif(not os.environ.get("LUMIERE_CREATIVE_TEST_DATA"), reason="opt-in real executable integration")
def test_motion_render_video_api_returns_downloadable_native_h264(client):
    created = client.post("/api/creative/title-card", json={"text": "API animated title", "width": 160,
                                                             "height": 90, "fps": 12, "duration": 1})
    assert created.status_code == 200, created.text
    creative_id = created.json()["id"]
    exported = client.post(f"/api/creative/{creative_id}/render-video", json={})
    assert exported.status_code == 200, exported.text
    body = exported.json()
    assert body["render_codec"] == "h264" and body["render_fps"] == pytest.approx(12)
    assert body["render_duration_seconds"] == pytest.approx(1)
    response = client.get(body["render_url"])
    assert response.status_code == 200 and response.headers["content-type"].startswith("video/mp4")
    assert response.content[:8][4:8] == b"ftyp"
