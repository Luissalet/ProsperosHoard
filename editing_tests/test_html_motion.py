from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
from io import BytesIO
from pathlib import Path
import time

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from conftest import FakeLink, make_config
from prosperos_hoard.editing.errors import Conflict, LumiereError
from prosperos_hoard.editing.html_motion import HtmlMotion, browser_executable
from prosperos_hoard.editing.main import create_app
from prosperos_hoard.editing.services import Services

SOURCE = """<!doctype html><html><meta charset='utf-8'><style>body{margin:0;background:#e00000}#box{position:absolute;top:20px;width:30px;height:30px;background:white}</style><div id='box'></div><script>window.seek=t=>{document.body.style.background=t<.5?'#e00000':'#0000e0';document.getElementById('box').style.left=(10+t*100)+'px';};seek(0);</script></html>"""
BROWSER = pytest.mark.skipif(importlib.util.find_spec("playwright") is None or not browser_executable(), reason="optional motion runtime and system browser not installed")


def save(store, **kwargs):
    return store.save(html=SOURCE, name="Motion QA", width=320, height=180, fps=10, duration=1, **kwargs)


def test_revisions_keep_source_and_restore(tmp_path):
    store = HtmlMotion(tmp_path)
    first = save(store)
    second = store.save(composition=first["id"], expected_revision=1, html=SOURCE + "<!-- correction -->", name="Corrected", width=640, height=360)
    assert store.source(first) == SOURCE
    assert store.get(first["id"])["revision"] == 2
    restored = store.restore(first["id"], 1, 2)
    assert restored["revision"] == 3
    assert restored["width"] == 320 and restored["fps"] == 10
    assert restored["source_sha256"] == first["source_sha256"]
    assert store.source(second).endswith("<!-- correction -->")
    assert len(store.list()["compositions"]) == 1


def test_concurrent_saves_preserve_winning_revision(tmp_path):
    store = HtmlMotion(tmp_path)
    first = save(store)
    def change(index):
        try:
            return store.save(composition=first["id"], expected_revision=1, html=SOURCE + str(index))
        except Conflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(change, [1, 2]))
    assert sum(r is not None for r in results) == 1
    assert store.get(first["id"])["revision"] == 2
    assert store.source(first) == SOURCE


@BROWSER
def test_storyboard_captures_real_sheet_and_original_frames_from_one_revision(tmp_path):
    store = HtmlMotion(tmp_path)
    saved = save(store)
    source = Path(saved['source_path']).read_bytes()
    result = store.storyboard(saved['id'], [0,.25,.75,1])
    assert result['revision']==1 and result['source_sha256']==saved['source_sha256']
    assert result['times']==[0,.25,.75,1] and len(result['frames'])==4
    sheet=store.storyboard_image(saved['id'],1,result['id'])
    assert hashlib.sha256(sheet).hexdigest()==result['image_sha256']
    with Image.open(BytesIO(sheet)) as image:assert image.size==(640,424)
    for i,frame in enumerate(result['frames']):
        data=store.storyboard_image(saved['id'],1,result['id'],i)
        assert hashlib.sha256(data).hexdigest()==frame['sha256']
        with Image.open(BytesIO(data)) as image:assert image.size==(320,180)
    assert len({f['sha256'] for f in result['frames']})>1
    assert Path(saved['source_path']).read_bytes()==source
    assert store.get(saved['id'])==saved


def test_storyboard_keeps_pinned_source_when_current_revision_changes(tmp_path,monkeypatch):
    import prosperos_hoard.editing.html_motion as module
    store=HtmlMotion(tmp_path);old=save(store);seen=[]
    def png(color):
        out=BytesIO();Image.new('RGB',(320,180),color).save(out,format='PNG');return out.getvalue()
    class Worker:
        def __init__(self,manifest):seen.append(manifest['revision'])
        def __enter__(self):return self
        def __exit__(self,*args):seen.append('closed')
        def frame(self,at):
            if store.get(old['id'])['revision']==1:
                store.save(composition=old['id'],expected_revision=1,html=SOURCE+'<!-- later -->')
            return png('red' if at==0 else 'blue')
    monkeypatch.setattr(module,'HtmlFrameWorker',Worker)
    result=store.storyboard(old['id'],[0,1])
    assert seen==[1,'closed'] and result['revision']==1
    assert result['source_sha256']==old['source_sha256'] and store.get(old['id'])['revision']==2
    assert store.source(old)==SOURCE
    assert store.storyboard_image(old['id'],1,result['id'])


@pytest.mark.parametrize('times', [[],[-1],[2],[float('nan')],[float('inf')]])
def test_bad_storyboard_times_are_rejected_before_worker(tmp_path,monkeypatch,times):
    import prosperos_hoard.editing.html_motion as module
    store=HtmlMotion(tmp_path);saved=save(store)
    def forbidden(*args):raise AssertionError('must not start a worker')
    monkeypatch.setattr(module,'HtmlFrameWorker',forbidden)
    with pytest.raises(LumiereError):store.storyboard(saved['id'],times)
    assert not (store.folder(saved['id'])/'1/storyboards').exists()


def test_storyboard_failure_closes_worker_without_publishing_partial_images(tmp_path,monkeypatch):
    import prosperos_hoard.editing.html_motion as module
    store=HtmlMotion(tmp_path);saved=save(store);closed=[]
    class Worker:
        def __init__(self,manifest):pass
        def __enter__(self):return self
        def __exit__(self,*args):closed.append(True)
        def frame(self,at):raise LumiereError('Injected render failure',code='html_motion_browser')
    monkeypatch.setattr(module,'HtmlFrameWorker',Worker)
    with pytest.raises(LumiereError):store.storyboard(saved['id'],[0,1])
    assert closed==[True] and not (store.folder(saved['id'])/'1/storyboards').exists()


@BROWSER
def test_storyboard_api_mcp_image_and_exact_archive_downloads(client):
    import base64
    from prosperos_hoard.editing.agent_tools import call_tool
    created=client.post('/api/html-motion',json={'html':SOURCE,'width':320,'height':180,'duration':1}).json()
    args={'composition':created['id'],'revision':1,'times':[0,.5,1]}
    response=client.post(f"/api/html-motion/{created['id']}/storyboard",json=args)
    assert response.status_code==200,response.text
    result=response.json();download=client.get(result['url'])
    assert download.status_code==200 and download.headers['cache-control']=='no-store'
    assert hashlib.sha256(download.content).hexdigest()==result['image_sha256']
    for frame in result['frames']:
        original=client.get(frame['url']);assert original.status_code==200
        assert hashlib.sha256(original.content).hexdigest()==frame['sha256']
    native=call_tool(client.app.state.services,'html_motion_storyboard',args)
    assert hashlib.sha256(base64.b64decode(native['_image']['data'])).hexdigest()==native['image_sha256']
    assert native['_image']['mime']=='image/png'


@BROWSER
def test_storyboard_default_times_and_tampered_asset_are_explicit(tmp_path):
    store=HtmlMotion(tmp_path);saved=save(store)
    result=store.storyboard(saved['id'])
    assert result['times'][0]==0 and result['times'][-1]==1 and len(result['frames'])==4
    folder=store.folder(saved['id'])/'1/storyboards'/result['id']
    (folder/'sheet.png').write_bytes(b'changed')
    with pytest.raises(LumiereError,match='hash'):store.storyboard_image(saved['id'],1,result['id'])
    with pytest.raises(LumiereError):store.storyboard_image(saved['id'],1,'../outside')


@BROWSER
def test_storyboard_reports_actual_source_bytes_changed_outside_saved_revision(tmp_path):
    store=HtmlMotion(tmp_path)
    red=b'<body style="margin:0;background:red"><script>window.seek=t=>{};</script></body>'
    blue=red.replace(b'background:red',b'background:blue')
    saved=store.save(html=red.decode(),width=64,height=36,duration=1)
    Path(saved['source_path']).write_bytes(blue)
    result=store.storyboard(saved['id'],[0])
    assert result['source_sha256']==hashlib.sha256(blue).hexdigest()
    assert result['saved_source_sha256']==hashlib.sha256(red).hexdigest()
    assert result['source_matches_revision'] is False
    with Image.open(BytesIO(store.storyboard_image(saved['id'],1,result['id'],0))) as image:
        assert image.getpixel((10,10))[:3]==(0,0,255)
    assert store.storyboard_source(saved['id'],1,result['id'])==blue
    assert Path(saved['source_path']).read_bytes()==blue


@BROWSER
def test_storyboard_worker_load_is_bound_to_bytes_before_concurrent_file_edit(tmp_path,monkeypatch):
    import prosperos_hoard.editing.html_motion as module
    store=HtmlMotion(tmp_path)
    red=b'<body style="margin:0;background:red"><script>window.seek=t=>{};</script></body>'
    blue=red.replace(b'background:red',b'background:blue')
    saved=store.save(html=red.decode(),width=64,height=36,duration=1)
    OriginalWorker=module.HtmlFrameWorker
    class EditingWorker(OriginalWorker):
        def __enter__(self):
            Path(saved['source_path']).write_bytes(blue)
            return super().__enter__()
    monkeypatch.setattr(module,'HtmlFrameWorker',EditingWorker)
    result=store.storyboard(saved['id'],[0,1])
    assert result['source_sha256']==hashlib.sha256(red).hexdigest() and result['source_matches_revision'] is True
    for i in range(2):
        with Image.open(BytesIO(store.storyboard_image(saved['id'],1,result['id'],i))) as image:
            assert image.getpixel((10,10))[:3]==(255,0,0)
    assert store.storyboard_source(saved['id'],1,result['id'])==red
    assert Path(saved['source_path']).read_bytes()==blue


@BROWSER
def test_captured_document_keeps_relative_resource_base(tmp_path):
    store=HtmlMotion(tmp_path)
    html='<body style="margin:0"><img src="asset.png"><script>window.seek=t=>{};</script></body>'
    saved=store.save(html=html,width=64,height=36,duration=1)
    Image.new('RGB',(64,36),'green').save(Path(saved['source_path']).parent/'asset.png')
    result=store.storyboard(saved['id'],[0])
    with Image.open(BytesIO(store.storyboard_image(saved['id'],1,result['id'],0))) as image:
        assert image.getpixel((10,10))[:3]==(0,128,0)


@pytest.mark.parametrize("params", [{"fps": float("nan")}, {"duration": float("inf")}, {"duration": 0}, {"width": 0}])
def test_invalid_geometry_and_timing(tmp_path, params):
    with pytest.raises(LumiereError):
        HtmlMotion(tmp_path).save(html=SOURCE, **params)


def test_api_and_tool_source_pagination(tmp_path):
    svc = Services(make_config(tmp_path), link=FakeLink(available=False), inline_jobs=True)
    app = create_app(svc.config, services=svc)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        first = client.post("/api/html-motion", json={"html": SOURCE, "name": "First"}).json()
        assert client.get(first["source_url"]).text == SOURCE
        assert client.get(f"/api/html-motion/{first['id']}").json()["html"] == SOURCE
        conflict = client.post("/api/html-motion", json={"html": "changed", "composition": first["id"], "expected_revision": 4})
        assert conflict.status_code == 409
        assert client.get("/api/html-motion").json()["compositions"][0]["revision"] == 1
        from prosperos_hoard.editing.agent_tools import run_html_motion_get, tool_catalog
        from prosperos_hoard.editing.html_motion_args import HtmlMotionGetArgs
        part = run_html_motion_get(svc, HtmlMotionGetArgs(composition=first["id"], offset=0, limit=20))
        assert part["html"] == SOURCE[:20] and part["next_offset"] == 20
        part2 = run_html_motion_get(svc, HtmlMotionGetArgs(composition=first["id"], offset=20))
        assert part["html"] + part2["html"] == SOURCE and part2["next_offset"] is None
        assert {t["name"] for t in tool_catalog()} >= {"html_motion_save", "html_motion_get", "html_motion_frame", "html_motion_render", "html_motion_restore", "html_motion_list"}


@BROWSER
def test_seek_out_of_order_and_fresh_browser_have_identical_pixels(tmp_path):
    store = HtmlMotion(tmp_path)
    manifest = save(store)
    with store.page(manifest) as (page, errors):
        late = store.capture(page, errors, .8)
        early = store.capture(page, errors, .2)
        assert late != early
        assert store.capture(page, errors, .8) == late
    fresh, receipt = store.frame(manifest["id"], .8)
    assert fresh == late
    assert receipt["frame_sha256"] == hashlib.sha256(late).hexdigest()
    assert Image.open(BytesIO(late)).getpixel((0, 0))[:3] == (0, 0, 224)
    assert Image.open(BytesIO(early)).getpixel((0, 0))[:3] == (224, 0, 0)


@BROWSER
@pytest.mark.parametrize("source,code", [("<html>No seek</html>", "html_motion_seek"), ("<script>window.seek=t=>{throw Error('bad frame')}</script>", "html_motion_browser")])
def test_broken_source_is_a_visible_error(tmp_path, source, code):
    store = HtmlMotion(tmp_path)
    manifest = store.save(html=source, width=160, height=90)
    with pytest.raises(LumiereError) as caught:
        store.frame(manifest["id"], .5)
    assert caught.value.code == code


@BROWSER
def test_real_export_keeps_queued_revision_when_source_changes(tmp_path):
    svc = Services(make_config(tmp_path), link=FakeLink(available=False), inline_jobs=False)
    store = HtmlMotion(svc.config.data_dir)
    first = save(store)
    queued = store.queue_render(svc, first["id"])
    changed = store.save(composition=first["id"], expected_revision=1, html=SOURCE.replace("#e00000", "#00e000"), name="Changed", width=320, height=180)
    svc.jobs._run(queued["job"])
    job = svc.jobs.get(queued["job"])
    assert job["state"] == "done", job["error"]
    result = job["result"]
    assert result["revision"] == 1 and result["source_sha256"] == first["source_sha256"]
    assert result["frames"] == 10 and result["render_duration"] == 1
    assert result["audio"] is False and result["codec"] == "h264"
    assert Path(result["render_path"]).stat().st_size > 1000
    assert store.get(first["id"])["revision"] == changed["revision"]
    assert store.get(first["id"])["render_url"] is None
    assert store.source(first) == SOURCE
    svc.stop()


@BROWSER
def test_second_canvas_source_and_png_route(tmp_path):
    svc = Services(make_config(tmp_path), link=FakeLink(available=False), inline_jobs=True)
    app = create_app(svc.config, services=svc)
    html = "<canvas id='c' width='180' height='320'></canvas><script>window.seek=t=>{const x=c.getContext('2d');x.fillStyle=t<1?'#20a060':'#8030c0';x.fillRect(0,0,180,320);};</script>"
    with TestClient(app, base_url="http://127.0.0.1") as client:
        manifest = client.post("/api/html-motion", json={"html": html, "width": 180, "height": 320, "fps": 5, "duration": 1.1, "name": "Canvas"}).json()
        response = client.get(manifest["frame_url"] + "?at=.2")
        assert response.status_code == 200 and response.headers["content-type"] == "image/png"
        assert Image.open(BytesIO(response.content)).size == (180, 320)
        queued = client.post(f"/api/html-motion/{manifest['id']}/render").json()
        job = svc.jobs.get(queued["job"])
        assert job["state"] == "done", job["error"]
        assert job["result"]["frames"] == 6 and job["result"]["render_duration"] == 1.2
        assert client.get(job["result"]["render_url"]).status_code == 200


@BROWSER
def test_cancel_unresolved_seek_releases_render_lane(tmp_path):
    svc = Services(make_config(tmp_path), link=FakeLink(available=False), emit_fn=lambda *args: None)
    svc.db.set_setting("notify.via", "off")
    store = HtmlMotion(svc.config.data_dir)
    blocked = store.save(html="<script>window.seek=async()=>await new Promise(()=>{});</script>", width=320, height=180, fps=10, duration=1)
    good = save(store)
    svc.start()
    try:
        first = store.queue_render(svc, blocked["id"])
        deadline = time.monotonic() + 15
        ctx = None
        while time.monotonic() < deadline:
            ctx = svc.jobs._running.get(first["job"])
            if ctx and ctx.handles and ctx.handles[0].proc is not None:
                break
            time.sleep(.05)
        assert ctx and ctx.handles[0].proc is not None, "browser seek did not start"
        second = store.queue_render(svc, good["id"])
        started = time.monotonic()
        svc.jobs.cancel(first["job"])
        assert svc.jobs.wait(first["job"], 4)["state"] == "canceled"
        assert time.monotonic() - started < 4
        result = svc.jobs.wait(second["job"], 15)
        assert result["state"] == "done", result["error"]
    finally:
        svc.stop()


@BROWSER
def test_frame_worker_timeout_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("LUMIERE_HTML_FRAME_TIMEOUT_S", "3")
    store = HtmlMotion(tmp_path)
    manifest = store.save(html="<script>window.seek=async()=>await new Promise(()=>{});</script>", width=160, height=90)
    started = time.monotonic()
    with pytest.raises(LumiereError) as error:
        store.frame(manifest["id"], .2)
    assert error.value.code == "html_motion_timeout"
    assert time.monotonic() - started < 8


@BROWSER
def test_mcp_frame_url_remains_bound_to_captured_revision(tmp_path, monkeypatch):
    from prosperos_hoard.editing.agent_tools import run_html_motion_frame
    from prosperos_hoard.editing.html_motion_args import HtmlMotionFrameArgs
    svc = Services(make_config(tmp_path), link=FakeLink(available=False), emit_fn=lambda *args: None)
    store = HtmlMotion(svc.config.data_dir)
    first = save(store)
    original = HtmlMotion.frame
    def concurrent_edit(self, *args, **kwargs):
        data, receipt = original(self, *args, **kwargs)
        self.save(composition=first["id"], expected_revision=1, html=SOURCE+"<!-- concurrent -->")
        return data, receipt
    monkeypatch.setattr(HtmlMotion, "frame", concurrent_edit)
    result = run_html_motion_frame(svc, HtmlMotionFrameArgs(composition=first["id"], at=.2, show=False))
    assert result["revision"] == 1 and "/revisions/1/frame" in result["url"]
    assert store.get(first["id"])["revision"] == 2
    svc.stop()


@BROWSER
def test_receive_imports_the_revision_visible_to_the_user(tmp_path):
    svc = Services(make_config(tmp_path), link=FakeLink(available=False), emit_fn=lambda *args: None, inline_jobs=True)
    svc.db.set_setting("notify.via", "off")
    app = create_app(svc.config, services=svc)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        store = HtmlMotion(svc.config.data_dir)
        first = save(store)
        j1 = store.queue_render(svc, first["id"])
        r1 = svc.jobs.get(j1["job"])["result"]
        second = store.save(composition=first["id"], expected_revision=1, html=SOURCE.replace("#e00000", "#00e000"), width=320, height=180, duration=1, fps=10)
        j2 = store.queue_render(svc, second["id"])
        r2 = svc.jobs.get(j2["job"])["result"]
        assert r1["render_path"] != r2["render_path"]
        received = client.post(f"/api/html-motion/{first['id']}/receive", json={"revision": 1})
        assert received.status_code == 200, received.text
        assert svc.db.one("SELECT id FROM media WHERE path = ?", (r1["render_path"],)) is not None
        assert svc.db.one("SELECT id FROM media WHERE path = ?", (r2["render_path"],)) is None
