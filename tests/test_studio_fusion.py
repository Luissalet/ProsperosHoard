"""Owned ASGI modules: synthetic stores only, no listener, model or browser."""
import asyncio
from contextlib import asynccontextmanager
import json
import subprocess
import sys

import httpx
import pytest

from prosperos_hoard.studio import Module, Studio, create_app
from prosperos_hoard.studio_projects import Projects
from prosperos_hoard.studio_response import HTML, NativeResponse, scoped


def test_cold_import_has_no_native_engines_or_store(tmp_path):
    code = '''
from pathlib import Path
import sys
from prosperos_hoard.studio import Studio
s = Studio(Path(sys.argv[1]))
assert len(s.tools) == 168
assert all(m.phase == 'off' for m in s.modules.values())
assert 'prosperos_hoard.api' not in sys.modules
assert 'prosperos_hoard.editing.main' not in sys.modules
assert not Path(sys.argv[1]).exists()
'''
    subprocess.run([sys.executable, '-c', code, str(tmp_path / 'cold')], check=True)


def test_project_revisions_survive_off_and_preserve_prior_snapshot(tmp_path):
    store = Projects(tmp_path)
    assert store.list() == []
    project = store.create('Escena')['id']
    one = store.link(project, 'generation', 'timeline', 't1', '/generation/#/p/p1/timeline/t1', {'clips': [1]})
    same = store.link(project, 'generation', 'timeline', 't1', '/generation/#/p/p1/timeline/t1', {'clips': [1]})
    two = store.link(project, 'generation', 'timeline', 't1', '/generation/#/p/p1/timeline/t1', {'clips': [1, 2]})
    assert same['id'] == one['id'] and two['revision'] != one['revision']
    rows = Projects(tmp_path).get(project)['objects']
    assert [r['snapshot']['clips'] for r in rows] == [[1], [1, 2]]


def test_urls_and_html_scope_once_without_changing_scripts():
    assert scoped('/api/media/m1/file', 'editing', 8815) == '/editing/api/media/m1/file'
    assert scoped('/editing/api/media/m1/file', 'editing', 8815) == '/editing/api/media/m1/file'
    parser = HTML('editing', 8815)
    parser.feed('<html><head><script>const x="/api/test";</script></head><body><video src="/api/media/m1/file"></video></body></html>')
    html = ''.join(parser.parts)
    assert 'src="/editing/api/media/m1/file"' in html
    assert 'const x="/api/test"' in html
    assert html.count('/studio-assets/native.js') == 1
    assert '/editing/editing/' not in html


@pytest.mark.asyncio
async def test_stream_disconnect_and_off_leave_other_module_responsive(tmp_path):
    started, stopped = asyncio.Event(), asyncio.Event()
    class App:
        router = None
        async def __call__(self, scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': [(b'content-type', b'video/mp4')]})
            started.set()
            try:
                while (await receive())['type'] != 'http.disconnect':
                    pass
            finally:
                stopped.set()
    module = Module('editing', None, tmp_path, 8815, False)
    app = App()
    @asynccontextmanager
    async def lifespan():
        yield
    module.app, module.context, module.phase = app, lifespan(), 'on'
    await module.context.__aenter__()
    upstream = asyncio.Queue()
    messages = []
    async def send(value): messages.append(value)
    stream = asyncio.create_task(module.request({'type': 'http'}, upstream.get, send))
    await started.wait()
    # No process-wide queue: another module's control completes with a held stream.
    other = Module('generation', None, tmp_path, 8815, False)
    assert (await other.set(False))['phase'] == 'off'
    await module.set(False)
    await stream
    assert stopped.is_set() and module.requests == {} and module.phase == 'off'


@pytest.mark.asyncio
async def test_http_discovery_and_projects_do_not_enable_modules(tmp_path):
    app = create_app(tmp_path, port=64239)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:64239') as client:
        assert len((await client.get('/api/agent/tools')).json()['tools']) == 168
        assert not tmp_path.joinpath('prosperos.sqlite3').exists()
        project = (await client.post('/api/studio/projects', json={'title': 'Corte'})).json()
        assert project['title'] == 'Corte'
        assert (await client.get('/api/studio/projects/'+project['id'])).json()['objects'] == []
        assert all(not m['enabled'] for m in (await client.get('/api/studio/modules')).json()['modules'])
        assert (await client.get('/editing/api/health')).status_code == 409


@pytest.mark.asyncio
async def test_common_writes_preserve_native_profiles_and_compact_journal(tmp_path):
    from prosperos_hoard.hoard_link.tokens import mint_agent_token
    studio = Studio(tmp_path, port=64239)
    master = studio.token()
    draft = mint_agent_token(tmp_path, 'synthetic929', 'drafts')['token']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=studio), base_url='http://127.0.0.1:64239') as client:
        refused = await client.post('/api/agent/call', headers={'Authorization':'Bearer '+draft}, json={
            'name':'studio_module_set','reason':'Resume requested work', 'arguments':{'module':'generation','enabled':True}})
        assert refused.status_code == 403 and studio.modules['generation'].phase == 'off'
        created = await client.post('/api/agent/call', headers={'Authorization':'Bearer '+master}, json={
            'name':'studio_project_create','reason':'Create a synthetic cut', 'arguments':{'title':'Synthetic cut'}})
        assert created.status_code == 200
    journal = json.loads((tmp_path/'agent_journal.jsonl').read_text(encoding='utf-8').splitlines()[-1])
    assert journal['args_digest'].startswith('sha256:') and journal['ids']
    assert 'arguments' not in journal and 'result' not in journal


@pytest.mark.asyncio
async def test_native_modules_transfer_and_reopen_use_isolated_stores(tmp_path, monkeypatch):
    from prosperos_hoard import api as native_generation
    from prosperos_hoard.editing import services, ffmpeg, media
    from prosperos_hoard.editing.hoard_link import family as editing_family
    from prosperos_hoard.hoard_link import family as generation_family
    monkeypatch.setattr(services.Services, '_build_link', lambda self: None)
    monkeypatch.setattr(editing_family, 'emit', lambda *a, **k: None)
    monkeypatch.setattr(generation_family, 'emit', lambda *a, **k: None)
    monkeypatch.setattr(services.Services, 'tools', lambda self: None)
    monkeypatch.setattr(ffmpeg, 'probe', lambda *a: {})
    monkeypatch.setattr(ffmpeg, 'summarize', lambda *a: dict(kind='image',duration_ms=0,width=64,height=64,
        fps=24,has_video=True,has_audio=False))
    monkeypatch.setattr(media, 'schedule_prepare', lambda *a: None)
    studio = Studio(tmp_path, port=64239)
    try:
        await studio.modules['generation'].set(True)
        await studio.modules['editing'].set(True)
        source = studio.modules['generation'].app
        store = source.state.store
        native_project = await asyncio.to_thread(store.create_project, 'Synthetic source')
        image = tmp_path / 'synthetic.png'
        image.write_bytes(b'fixture-probe-is-stubbed-no-media-executable')
        asset = await asyncio.to_thread(store.create_asset, native_project['id'], 'image', str(image), width=64,height=64)
        timeline = await asyncio.to_thread(store.create_timeline, native_project['id'], 'Synthetic cut', tracks=[
            {'type':'visual','clips':[{'asset_id':asset['id'],'duration_s':2}]}])
        project = studio.projects.create('Shared synthetic work')['id']
        result = await studio.host_tool('studio_project_transfer', {'project':project,'timeline_id':timeline['id']})
        assert len(result['transfers']) == 1
        assert {o['module'] for o in result['objects']} == {'generation','editing'}
        target = next(o for o in result['objects'] if o['module']=='editing')
        target_id = target['native_id']
        assert target['route'] == '/editing/#/p/' + target_id
        assert result['transfers'][0]['receipt']['result']['clips'] == 1
        await studio.close()
        assert all(m.phase=='off' and not m.requests for m in studio.modules.values())
        assert studio.projects.get(project)['objects'] == result['objects']
        await studio.modules['editing'].set(True)
        row = await asyncio.to_thread(studio.modules['editing'].app.state.services.db.one,
            'SELECT id FROM projects WHERE id=?', (target_id,))
        assert row['id'] == target_id
    finally:
        await studio.close()
