"""Native-name compatibility inside the owner; discovery never starts engines.

Run ``python -m prosperos_hoard.compat_mcp --module editing`` for the former native
MCP connection. HTTP aliases use ``/compat/<module>`` on the same owner.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

OWNER = 'prospero'
ENV = 'PROSPERO'
DEFAULT_URL = 'http://127.0.0.1:8815'
SERVICE = 'prosperos-hoard'
CATALOG = 'studio_catalog.json'
MODULES = ('generation', 'editing')


def entries(module):
    """Read only shipped schema bytes, without importing a native package."""
    if module not in MODULES:
        raise ValueError('Unknown native module: ' + str(module))
    data = json.loads((Path(__file__).parent / CATALOG).read_text(encoding='utf-8'))
    rows = data[module] if isinstance(data, dict) else [row for row in data if row['module'] == module]
    return rows


def native_catalog(module):
    return [dict({key: value for key, value in row.items() if key not in ('module', 'native_name', 'rest')},
                 name=row.get('native_name', row['name'])) for row in entries(module)]


def translate(module, body):
    """Keep caller context/arguments; translate only the selected tool identity."""
    if not isinstance(body, dict):
        raise ValueError('Expected a JSON object')
    name = body.get('name') or body.get('tool')
    row = next((row for row in entries(module) if row.get('native_name', row['name']) == name), None)
    if row is None:
        raise LookupError('Unknown native tool')
    result = dict(body, name=row['name'])
    if 'tool' in result:
        result['tool'] = row['name']
    return result


async def json_response(send, body, status=200):
    data = json.dumps(body, ensure_ascii=False).encode('utf-8')
    await send({'type': 'http.response.start', 'status': status,
                'headers': [(b'content-type', b'application/json; charset=utf-8'),
                            (b'content-length', str(len(data)).encode())]})
    await send({'type': 'http.response.body', 'body': data})


class Compatibility:
    """Same-process aliases, retaining the owner's authorization and lifecycle."""
    def __init__(self, app):
        self.app = app

    def __getattr__(self, name):
        return getattr(self.app, name)

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        if scope['type'] != 'http' or not path.startswith('/compat/'):
            return await self.app(scope, receive, send)
        parts = path.split('/', 3)
        module = parts[2] if len(parts) > 2 else ''
        tail = '/' + parts[3] if len(parts) == 4 else '/'
        if module not in MODULES:
            return await json_response(send, {'error': 'unknown_module'}, 404)
        if scope['method'] == 'GET' and tail == '/api/agent/tools':
            return await json_response(send, {'service': SERVICE, 'module': module, 'tools': native_catalog(module),
                                             'instructions': 'Native module names. Discovery starts nothing. Enable the module explicitly in the owner before calling its tools.'})
        if scope['method'] == 'GET' and tail == '/api/health':
            return await json_response(send, {'ok': True, 'service': SERVICE, 'owner': OWNER, 'module': module, 'demand_only': True})
        if scope['method'] != 'POST' or tail != '/api/agent/call':
            return await json_response(send, {'error': 'not_found'}, 404)
        chunks = []
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            chunks.append(message.get('body', b''))
            if not message.get('more_body', False):
                break
        try:
            body = translate(module, json.loads(b''.join(chunks)))
        except (ValueError, TypeError):
            return await json_response(send, {'error': 'invalid_request'}, 422)
        except LookupError:
            return await json_response(send, {'error': 'unknown_tool'}, 404)
        data = json.dumps(body).encode('utf-8')
        child = dict(scope, path='/api/agent/call', raw_path=b'/api/agent/call')
        child['headers'] = [(key, value) for key, value in scope.get('headers', []) if key != b'content-length'] + [(b'content-length', str(len(data)).encode())]
        delivered = False
        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': data, 'more_body': False}
            return await receive()
        return await self.app(child, replay, send)


def install(app):
    if hasattr(app, 'add_middleware'):
        app.add_middleware(Compatibility)
        return app
    return Compatibility(app)


def settings():
    url = os.environ.get(ENV + '_URL', DEFAULT_URL).rstrip('/')
    parsed = urlsplit(url)
    if parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1', '::1') or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(ENV + '_URL must be an HTTP loopback URL')
    explicit = os.environ.get(ENV + '_TOKEN_FILE') or os.environ.get('HOARD_TOKEN_FILE')
    profile = os.environ.get(ENV + '_PROFILE_DIR') or os.environ.get(ENV + '_DATA_DIR')
    token_file = Path(explicit) if explicit else Path(profile or Path(__file__).parent.parent / 'data') / 'mcp-token'
    token = os.environ.get(ENV + '_TOKEN') or token_file.read_text(encoding='utf-8-sig').strip()
    return url, token


def context_values(meta=None):
    values = meta.model_dump(exclude_none=True) if hasattr(meta, 'model_dump') else dict(meta or {})
    headers, body = {}, {}
    for key, env, header, limit in [('agent', 'HOARD_AGENT_ID', 'x-agent-id', 80),
                                     ('session', 'HOARD_AGENT_SESSION', 'x-agent-session', 120)]:
        value = values.get('hoard/' + key) or os.environ.get(env, '')
        value = ''.join(char for char in str(value) if 32 <= ord(char) < 127).strip()[:limit]
        if value:
            headers[header], body[key] = value, value
    for key in ('caller', 'reason'):
        if values.get('hoard/' + key):
            body[key] = values['hoard/' + key]
    return headers, body


async def call_native(module, name, arguments, *, meta=None, client_factory=None):
    """One HTTP attempt; an uncertain write is never retried or activated."""
    import httpx
    from mcp.types import CallToolResult, ImageContent, TextContent
    row = next((row for row in entries(module) if row.get('native_name', row['name']) == name), None)
    if row is None:
        return CallToolResult(content=[TextContent(type='text', text='Unknown native tool')], isError=True)
    write = not row.get('annotations', {}).get('readOnlyHint', False)
    sent = False
    try:
        url, token = settings()
        headers, context = context_values(meta)
        payload = {'name': name, 'arguments': dict(arguments or {}), **context}
        if 'reason' not in payload and isinstance(arguments, dict) and arguments.get('reason'):
            payload['reason'] = arguments['reason']
        async def trace(event, info):
            nonlocal sent
            if event.endswith('send_request_headers.started'):
                sent = True
        async with (client_factory or httpx.AsyncClient)(trust_env=False, follow_redirects=False,
                                                        timeout=row.get('x-timeout-s', 1800)) as client:
            request = client.build_request('POST', url + '/compat/' + module + '/api/agent/call',
                                           headers={'authorization': 'Bearer ' + token, **headers}, json=payload)
            request.extensions['trace'] = trace
            response = await client.send(request)
            result = response.json()
        if response.status_code >= 400:
            if write and response.status_code >= 500:
                result = {'code': 'outcome_unknown', 'outcome_unknown': True,
                          'reconcile_action': 'read_current_state_before_retry', 'native_error': result}
            return CallToolResult(content=[TextContent(type='text', text=json.dumps(result, ensure_ascii=False))], isError=True)
        if isinstance(result, dict) and isinstance(result.get('content'), list):
            return CallToolResult.model_validate(result)
        picture = result.pop('_image', None) if isinstance(result, dict) else None
        if OWNER == 'vitruvius' and name == 'theseus_artifact_image' and isinstance(result, dict):
            native = result.get('result', {})
            if native.get('base64'):
                picture = {'data': native.pop('base64'), 'mime': native.get('media_type', 'image/png')}
        content = [TextContent(type='text', text=json.dumps(result, ensure_ascii=False))]
        if isinstance(picture, dict) and picture.get('data'):
            content.append(ImageContent(type='image', data=picture['data'], mimeType=picture.get('mime', 'image/jpeg')))
        return CallToolResult(content=content)
    except (OSError, ValueError, httpx.HTTPError) as error:
        uncertain = bool(sent and write)
        failure = {'code': 'outcome_unknown' if uncertain else 'owner_unavailable',
                   'error': type(error).__name__, 'outcome_unknown': uncertain,
                   'reconcile_action': 'read_current_state_before_retry' if uncertain else 'start_owner_explicitly'}
        return CallToolResult(content=[TextContent(type='text', text=json.dumps(failure))], isError=True)


async def serve(module):
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool
    server = Server(OWNER + '-' + module)
    @server.list_tools()
    async def tools():
        return [Tool(**row) for row in native_catalog(module)]
    @server.call_tool()
    async def call(name, arguments):
        result = await call_native(module, name, arguments, meta=server.request_context.meta)
        # MCP 1.9 expects a content sequence; later versions also accept the
        # result model. Raising preserves isError on both protocol versions.
        if result.isError:
            raise ValueError('\n'.join(getattr(item, 'text', '') for item in result.content))
        return result.content
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--module', choices=MODULES, required=True)
    args = parser.parse_args()
    asyncio.run(serve(args.module))


if __name__ == '__main__':
    main()
