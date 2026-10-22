"""Verify installed owned resources and synthetic native lifetimes outside source."""
import argparse
import asyncio
from contextlib import nullcontext
import hashlib
from html.parser import HTMLParser
import importlib.metadata
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from zipfile import ZipFile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--installed', type=Path, required=True)
    parser.add_argument('--wheel', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    target, wheel = args.installed.resolve(), args.wheel.resolve()
    if args.receipt.exists():
        raise FileExistsError('Use a fresh receipt name')
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    assert not (Path.cwd() / 'prosperos_hoard').exists(), 'Run from outside the source tree'
    sys.path.insert(0, str(target))
    from prosperos_hoard.resources import generation_ui, app_icon, editing_engine, installed
    from prosperos_hoard.studio import Studio
    assert installed() and generation_ui().is_relative_to(target) and app_icon().is_relative_to(target)
    manifest = json.loads((target / 'prosperos_hoard/resources/manifest.json').read_text(encoding='utf-8'))
    with ZipFile(wheel) as archive:
        names = archive.namelist()
        # A platform wheel may place pure Python/resources in its purelib scheme.
        members = {name.split('.data/purelib/',1)[-1]:name for name in names}
        forbidden = {'data', 'node_modules', '.venv', '__pycache__', 'target', '.env'}
        assert not any(forbidden.intersection(Path(name).parts) or name.endswith('.pyc') for name in names)
        metadata = archive.read(next(name for name in names if name.endswith('.dist-info/WHEEL'))).decode()
        assert 'Root-Is-Purelib: false' in metadata and 'Tag: py3-none-win_amd64' in metadata
        for row in manifest['files']:
            relative = 'prosperos_hoard/resources/' + row['path']
            content = archive.read(members[relative])
            assert len(content) == row['bytes'] and hashlib.sha256(content).hexdigest() == row['sha256']
            assert (target / relative).read_bytes() == content
        for required in ['prosperos_hoard/studio_catalog.json', 'prosperos_hoard/studio_mcp_catalog.json',
            'prosperos_hoard/hoard_link/_data/family-services.json', 'prosperos_hoard/editing/hoard_link/_data/family-services.json',
            'prosperos_hoard/fonts/Inter/Inter.ttf', 'prosperos_hoard/studio_ui/fonts/cinzel-LICENSE.txt',
            'prosperos_hoard/editing/static/index.html', 'prosperos_hoard/resources/generation-ui/index.html']:
            assert required in members, required
    cold_events = []
    def audit(event, arguments):
        if event in ('sqlite3.connect', 'subprocess.Popen'):
            cold_events.append(event)
    sys.addaudithook(audit)
    # Retain synthetic receipts/state for review; never remove a live worker cwd.
    with nullcontext(tempfile.mkdtemp(prefix='qa-installed929-',dir=args.receipt.parent)) as temporary:
        private = Path(temporary)
        os.environ.update({'HOARD_HOME':str(private/'hoard-home'), 'HOARD_EVENTS':'0',
            'HOARD_HUB_AUTOSTART':'0', 'PROSPERO_GPU_LEASE':'0', 'HOARD_COPILOT':'0'})
        cold = Studio(private/'not-created', port=64839)
        assert not cold.root.exists() and len(cold.tools) == 181 and not cold_events
        assert 'prosperos_hoard.api' not in sys.modules and 'prosperos_hoard.editing.main' not in sys.modules
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        mcp_audit = private/'mcp-audit.json'
        entry = """import json,runpy,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
events=[]
def observe(name,args):
    if name in ('sqlite3.connect','subprocess.Popen'):events.append(name)
sys.addaudithook(observe)
import prosperos_hoard.mcp_server as server
from mcp.types import ListToolsRequest
original=server.mcp._mcp_server.request_handlers[ListToolsRequest]
async def audited(*args,**kwargs):
    result=await original(*args,**kwargs)
    native=[name for name in sys.modules if name in ('prosperos_hoard.api','prosperos_hoard.editing.main')]
    Path(sys.argv[2]).write_text(json.dumps({'events':events,'native_imports':native}),encoding='utf-8')
    return result
server.mcp._mcp_server.request_handlers[ListToolsRequest]=audited
server.main()
"""
        async def discover():
            env = {**os.environ,'PROSPERO_DATA_DIR':str(private/'cold-mcp'),
                   'PROSPERO_TOKEN_FILE':str(private/'cold-mcp/mcp-token'),'PROSPERO_URL':'http://127.0.0.1:64839'}
            parameters = StdioServerParameters(command=sys.executable,args=['-I','-c',entry,str(target),str(mcp_audit)],cwd=str(private),env=env)
            async with stdio_client(parameters) as (reader,writer):
                async with ClientSession(reader,writer) as session:
                    await session.initialize()
                    actual = (await session.list_tools()).tools
                    assert len(actual) == 181 and {tool.name for tool in actual} == set(cold.tools)
        asyncio.run(discover())
        assert not (private/'cold-mcp').exists()
        assert json.loads(mcp_audit.read_text(encoding='utf-8')) == {'events':[],'native_imports':[]}
        from prosperos_hoard import __main__ as launcher
        destinations = []
        launcher.uvicorn.run = lambda app,**kwargs: destinations.append(app.studio.root)
        launcher._setup_logging = lambda root: None
        os.environ['LOCALAPPDATA'] = str(private/'local-appdata')
        os.environ.pop('PROSPERO_DATA_DIR',None)
        sys.argv = ['prosperos-hoard','--no-browser']
        launcher.main()
        assert destinations[-1] == private/'local-appdata/ProsperosHoard/data'
        explicit = private/'existing-owner/data'
        explicit.parent.mkdir()
        (explicit.parent/'.env').write_text('PROSPERO_PACKAGE_FIXTURE=owned\n',encoding='utf-8')
        sys.argv = ['prosperos-hoard','--data-dir',str(explicit),'--no-browser']
        launcher.main()
        assert destinations[-1] == explicit and os.environ['PROSPERO_PACKAGE_FIXTURE'] == 'owned'
        from fastapi.testclient import TestClient
        from prosperos_hoard.studio import create_app
        # Native stores/apps remain real; external family inference/events are inert.
        from prosperos_hoard.hoard_link import family as generation_family
        from prosperos_hoard.editing.hoard_link import family as editing_family
        from prosperos_hoard.editing.services import Services
        generation_family.emit = editing_family.emit = lambda *a, **k: False
        Services._build_link = lambda self: None
        app = create_app(private/'data', port=64839)
        checked = []
        class Links(HTMLParser):
            def handle_starttag(self, tag, attributes):
                for key, value in attributes:
                    if key in ('src', 'href') and value and value.startswith('/'):
                        if not value.startswith('/api/copilot/'):
                            checked.append(value)
        with TestClient(app, base_url='http://127.0.0.1:64839') as client:
            def get(url):
                response = client.get(url)
                assert response.status_code == 200, (url, response.status_code, response.text[:200])
                return response
            assert all(not m['enabled'] for m in get('/api/studio/modules').json()['modules'])
            assert len(get('/api/agent/tools').json()['tools']) == 181
            Links().feed(get('/').text)
            for module in ('generation', 'editing'):
                response = client.put('/api/studio/modules/'+module,json={'enabled':True,'automatic':False})
                assert response.status_code == 200, response.text
                html = get('/'+module+'/').text
                assert '<html' in html.lower() and len(html) > 256
                Links().feed(html)
            for url in set(checked):
                response = get(url)
                if url.endswith('.css'):
                    for asset in re.findall(r'url\(["\']?([^"\')]+)', response.text):
                        if asset.startswith('/'):
                            get(asset)
                        elif not asset.startswith('data:'):
                            from urllib.parse import urljoin
                            get(urljoin(url,asset))
            for module in ('generation','editing'):
                assert client.put('/api/studio/modules/'+module,json={'enabled':False}).status_code == 200
            assert all(not m['enabled'] for m in get('/api/studio/modules').json()['modules'])
        from prosperos_hoard.editing.creative_engines import CreativeEngines
        engines = CreativeEngines(private/'native-data', port=64839)
        tools = {engine: engines.tools(engine)['count'] for engine in ('motion','edit')}
        assert tools == {'motion':21,'edit':9}, tools
        # Real owned worker: create/save/reopen a small editable composition, no rendering/GPU/model.
        created = engines._begin('motion', None)
        folder, native = Path(created['project_dir']), Path(created['native_path'])
        calls = [{'tool':'execute_command','arguments':{'command':'comp.new','params':{'name':'Installed package fixture','width':320,'height':180,'frameRate':10,'duration':1}}},
                 {'tool':'save_project','arguments':{'path':str(native)}}]
        result = asyncio.run(engines._request('motion',folder,calls))
        assert not any(row['is_error'] for row in result), result
        assert native.is_file() and native.stat().st_size > 0
        engines._finish(created,calls)
        before = native.read_bytes()
        reopened = engines.call(created['id'],[{'tool':'get_comp','arguments':{}}])
        assert reopened['ok'] and native.is_file(), reopened
        project = engines.get(created['id'])
        assert project['id'] == created['id'] and Path(project['native_path']) == native
        native_receipt = {'id':created['id'],'format':native.suffix,'bytes':native.stat().st_size,
                          'sha256':hashlib.sha256(native.read_bytes()).hexdigest(),
                          'initial_sha256':hashlib.sha256(before).hexdigest(),'reopened':True}
        for name, module in sys.modules.items():
            location = getattr(module,'__file__',None)
            if name.startswith('prosperos_hoard') and location:
                assert Path(location).resolve().is_relative_to(target), (name,location)
        receipt = {'status':'PASS','wheel':wheel.name,'wheel_sha256':hashlib.sha256(wheel.read_bytes()).hexdigest(),
            'wheel_files':len(names),'wheel_bytes':wheel.stat().st_size,'resource_manifest_files':len(manifest['files']),
            'installed':str(target),'synthetic_qa_retained':str(private),'cold_tools':181,'cold_sqlite_or_process_events':[],
            'installed_stdio_mcp_cold_tools':181,
            'both_modules_on_off':True,'checked_assets':sorted(set(checked)), 'native_tool_counts':tools,
            'installed_launcher_default_and_explicit_profile':True,
            'native_project':native_receipt,'gpu_model_render_browser_network_used':False,
            'versions':{key:importlib.metadata.version(key) for key in ('fastapi','starlette','pydantic','httpx')}}
    args.receipt.parent.mkdir(parents=True,exist_ok=True)
    args.receipt.write_text(json.dumps(receipt,indent=2),encoding='utf-8')
    print(json.dumps(receipt,indent=2))


if __name__ == '__main__':
    main()
