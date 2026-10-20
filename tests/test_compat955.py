"""Native connection regression: cold schemas, identity, OFF and one-attempt calls."""
import asyncio
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = 'prosperos_hoard'
spec = importlib.util.spec_from_file_location('compat955', ROOT / PACKAGE / 'compat_mcp.py')
compat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compat)


async def request(app, method, path, body=None, headers=None):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:64852') as client:
        return await client.request(method, path, json=body, headers=headers)


@pytest.mark.parametrize('module', compat.MODULES)
def test_cold_schema_is_native_and_does_not_dispatch(module, tmp_path, monkeypatch):
    absent = tmp_path / 'must-not-exist'
    monkeypatch.setenv(compat.ENV + '_DATA_DIR', str(absent))
    async def forbidden(*args):
        raise AssertionError('Discovery reached the owner or a native engine')
    before = set(sys.modules)
    response = asyncio.run(request(compat.Compatibility(forbidden), 'GET', '/compat/' + module + '/api/agent/tools'))
    assert response.status_code == 200
    assert response.json()['tools'] == compat.native_catalog(module)
    for native, row in zip(response.json()['tools'], compat.entries(module)):
        assert native['name'] == row.get('native_name', row['name'])
        assert native['inputSchema'] == row['inputSchema']
        assert native.get('annotations') == row.get('annotations')
    assert not absent.exists()
    added = set(sys.modules) - before
    assert not any(name.startswith(('torch', 'numpy', 'PIL', 'sqlite3')) for name in added)
    health = asyncio.run(request(compat.Compatibility(forbidden), 'GET', '/compat/' + module + '/api/health'))
    assert health.status_code == 200
    assert health.json()['service'] == compat.SERVICE


@pytest.mark.parametrize('module', compat.MODULES)
def test_alias_forwards_identity_context_and_off_error(module):
    row = compat.entries(module)[0]
    original_name = row.get('native_name', row['name'])
    seen = []
    async def owner(scope, receive, send):
        body = json.loads((await receive())['body'])
        seen.append((scope, body))
        await compat.json_response(send, {'error': 'module_disabled', 'module': module}, 409)
    payload = {'name': original_name, 'tool': original_name, 'arguments': {'id': 'unchanged-id', 'path': 'C:/unchanged/source'},
               'caller': 'family', 'agent': 'synthetic-agent', 'session': 'original-session', 'reason': 'Preserve native identity'}
    headers = {'authorization': 'Bearer synthetic-owner-token', 'x-agent-id': 'synthetic-agent', 'x-agent-session': 'original-session'}
    response = asyncio.run(request(compat.Compatibility(owner), 'POST', '/compat/' + module + '/api/agent/call', payload, headers))
    assert response.status_code == 409
    assert response.json() == {'error': 'module_disabled', 'module': module}
    assert len(seen) == 1
    scope, body = seen[0]
    assert scope['path'] == '/api/agent/call'
    assert body == dict(payload, name=row['name'], tool=row['name'])
    assert dict(scope['headers'])[b'authorization'] == b'Bearer synthetic-owner-token'
    assert dict(scope['headers'])[b'x-agent-session'] == b'original-session'
    assert int(dict(scope['headers'])[b'content-length']) == len(json.dumps(body).encode())


def test_unknown_tool_never_dispatches():
    async def forbidden(*args):
        raise AssertionError('Unknown native identity reached the owner')
    app = compat.Compatibility(forbidden)
    response = asyncio.run(request(app, 'POST', '/compat/' + compat.MODULES[0] + '/api/agent/call', {'name': 'not-a-tool'}))
    assert response.status_code == 404
    response = asyncio.run(request(app, 'POST', '/compat/' + compat.MODULES[0] + '/api/agent/call', ['not-an-object']))
    assert response.status_code == 422


def test_mcp_call_preserves_error_and_context(tmp_path, monkeypatch):
    module = compat.MODULES[-1]
    row = compat.entries(module)[0]
    token = tmp_path / 'owner-token'
    token.write_text('synthetic-owner-token', encoding='utf-8')
    monkeypatch.setenv(compat.ENV + '_TOKEN_FILE', str(token))
    monkeypatch.setenv(compat.ENV + '_URL', 'http://127.0.0.1:64852')
    monkeypatch.setenv('HOARD_AGENT_ID', 'agent-from-env')
    monkeypatch.setenv('HOARD_AGENT_SESSION', 'session-from-env')
    seen = []
    def reply(req):
        seen.append(req)
        return httpx.Response(409, json={'error': 'module_disabled', 'module': module})
    def client(**kwargs):
        assert kwargs['trust_env'] is False and kwargs['follow_redirects'] is False
        return httpx.AsyncClient(transport=httpx.MockTransport(reply), **kwargs)
    result = asyncio.run(compat.call_native(module, row.get('native_name', row['name']), {'id': 'native-id'},
                                           meta={'hoard/session': 'session-from-request', 'hoard/reason': 'Keep original context'}, client_factory=client))
    assert result.isError
    assert json.loads(result.content[0].text) == {'error': 'module_disabled', 'module': module}
    assert len(seen) == 1
    req = seen[0]
    assert req.url.path == '/compat/' + module + '/api/agent/call'
    assert req.headers['authorization'] == 'Bearer synthetic-owner-token'
    assert req.headers['x-agent-id'] == 'agent-from-env'
    assert req.headers['x-agent-session'] == 'session-from-request'
    body = json.loads(req.content)
    assert body['arguments'] == {'id': 'native-id'}
    assert body['reason'] == 'Keep original context'


def test_sent_write_is_uncertain_and_never_repeated(tmp_path, monkeypatch):
    module, row = next((module, row) for module in compat.MODULES for row in compat.entries(module)
                       if not row.get('annotations', {}).get('readOnlyHint', False))
    token = tmp_path / 'owner-token'
    token.write_text('synthetic-token', encoding='utf-8')
    monkeypatch.setenv(compat.ENV + '_TOKEN_FILE', str(token))
    seen = []
    async def reply(req):
        seen.append(req)
        await req.extensions['trace']('http11.send_request_headers.started', {})
        raise httpx.ReadError('synthetic loss after write', request=req)
    def client(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(reply), **kwargs)
    result = asyncio.run(compat.call_native(module, row.get('native_name', row['name']), {}, client_factory=client))
    assert len(seen) == 1
    assert result.isError and json.loads(result.content[0].text)['code'] == 'outcome_unknown'


@pytest.mark.parametrize('payload', [
    {'content': [{'type': 'image', 'data': 'c3ludGhldGlj', 'mimeType': 'image/png'}], 'isError': False},
    {'object_id': 'unchanged-object', '_image': {'data': 'c3ludGhldGlj', 'mime': 'image/png'}},
])
def test_native_images_remain_mcp_images(payload, tmp_path, monkeypatch):
    token = tmp_path / 'owner-token'
    token.write_text('synthetic-token', encoding='utf-8')
    monkeypatch.setenv(compat.ENV + '_TOKEN_FILE', str(token))
    module = compat.MODULES[-1]
    name = compat.native_catalog(module)[0]['name']
    def client(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload)), **kwargs)
    result = asyncio.run(compat.call_native(module, name, {}, client_factory=client))
    assert not result.isError
    pictures = [item for item in result.content if item.type == 'image']
    assert len(pictures) == 1 and pictures[0].data == 'c3ludGhldGlj' and pictures[0].mimeType == 'image/png'


def test_cancellation_closes_client_without_repeating_write(tmp_path, monkeypatch):
    token = tmp_path / 'owner-token'
    token.write_text('synthetic-token', encoding='utf-8')
    monkeypatch.setenv(compat.ENV + '_TOKEN_FILE', str(token))
    module, row = next((module, row) for module in compat.MODULES for row in compat.entries(module)
                       if not row.get('annotations', {}).get('readOnlyHint', False))
    clients, requests = [], []
    async def run():
        entered = asyncio.Event()
        async def blocked(req):
            requests.append(req)
            await req.extensions['trace']('http11.send_request_headers.started', {})
            entered.set()
            await asyncio.Event().wait()
        def client(**kwargs):
            value = httpx.AsyncClient(transport=httpx.MockTransport(blocked), **kwargs)
            clients.append(value)
            return value
        task = asyncio.create_task(compat.call_native(module, row.get('native_name', row['name']), {}, client_factory=client))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    assert len(requests) == 1 and len(clients) == 1 and clients[0].is_closed


def test_real_stdio_discovery_when_owner_is_closed(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    module = compat.MODULES[-1]
    absent = tmp_path / 'no-profile'
    async def run():
        params = StdioServerParameters(command=sys.executable, args=['-m', PACKAGE + '.compat_mcp', '--module', module],
                                      env={**os.environ, 'PYTHONPATH': str(ROOT), compat.ENV + '_DATA_DIR': str(absent),
                                           compat.ENV + '_PROFILE_DIR': str(absent), compat.ENV + '_TOKEN_FILE': str(absent / 'mcp-token'),
                                           compat.ENV + '_URL': 'http://127.0.0.1:64852'})
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                listed = await session.list_tools()
                assert [tool.name for tool in listed.tools] == [tool['name'] for tool in compat.native_catalog(module)]
                callable_tool = next(tool for tool in listed.tools if not tool.inputSchema.get('required'))
                result = await session.call_tool(callable_tool.name, {})
                assert result.isError
                assert 'owner_unavailable' in result.content[0].text
    asyncio.run(run())
    assert not absent.exists()


def test_real_owner_factory_keeps_native_modules_off(tmp_path, monkeypatch):
    profile = tmp_path / 'private-profile'
    profile.mkdir()
    token = profile / 'mcp-token'
    token.write_text('9' * 64, encoding='utf-8')
    monkeypatch.setenv(compat.ENV + '_PROFILE_DIR', str(profile))
    monkeypatch.setenv(compat.ENV + '_DATA_DIR', str(profile))
    monkeypatch.setenv(compat.ENV + '_TOKEN_FILE', str(token))
    monkeypatch.setenv(compat.ENV + '_PORT', '64852')
    sys.path.insert(0, str(ROOT))
    def forbidden(*args, **kwargs):
        raise AssertionError('Native factory was invoked implicitly')
    if compat.OWNER == 'vulcan':
        factory = importlib.import_module(PACKAGE + '.studio.app').create_app
        app = factory(profile, port=64852, factory=forbidden)
        modules = app.state.modules
    elif compat.OWNER in ('prospero', 'daguerre'):
        factory = importlib.import_module(PACKAGE + '.studio').create_app
        app = factory(profile, port=64852, factories={name: forbidden for name in compat.MODULES})
        modules = app.studio.modules
    else:
        path = '.consolidation.host' if compat.OWNER == 'coase' else '.software.host'
        app = importlib.import_module(PACKAGE + path).create_app()
        modules = app.state.runtime.modules
        for module in modules.values():
            monkeypatch.setattr(module, 'create', forbidden, raising=False)
    async def run():
        for module in compat.MODULES:
            base = '/compat/' + module
            listed = await request(app, 'GET', base + '/api/agent/tools')
            assert listed.status_code == 200
            assert listed.json()['tools'] == compat.native_catalog(module)
            native = next((row for row in compat.native_catalog(module) if not row['inputSchema'].get('required')), None)
            if native:
                response = await request(app, 'POST', base + '/api/agent/call', {'name': native['name'], 'arguments': {}},
                                         {'authorization': 'Bearer ' + '9' * 64})
                assert response.status_code >= 400 and response.status_code != 401
        assert all(getattr(module, 'app', None) is None for module in modules.values())
        rejected = await request(app, 'GET', '/compat/' + compat.MODULES[0] + '/api/agent/tools', headers={'host': 'unrelated.invalid'})
        assert rejected.status_code in (400, 403)
    asyncio.run(run())
