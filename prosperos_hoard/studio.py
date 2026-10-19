"""Cold Prospero ASGI owner. Engines and native views load only after explicit ON.

No HTTP relay or retired app process: the complete native ASGI apps execute here.
Each module owns its transition and active requests; streams never hold a global lock.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import secrets
from typing import Any

from .studio_projects import Projects
from .studio_contract import NAMES as HOST_TOOLS
from .cancellation import join_tasks
from .resources import generation_ui, app_icon

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = Path(__file__).with_name('studio_catalog.json')


def catalog():
    return json.loads(CATALOG_PATH.read_text(encoding='utf-8'))


async def generation(root, port, demo, automatic):
    from .api import create_app
    app = await asyncio.to_thread(create_app, root, generation_ui(), port, demo,
                                  resume_pending=automatic, url_prefix='/generation')
    return app


async def editing(root, port, demo, automatic):
    from dataclasses import replace
    import os
    from .editing.config import Config
    from .editing.guard import parse_allowed_hosts
    from .editing.main import create_app
    config = Config.from_env()
    allowed = tuple(dict.fromkeys((*config.allowed_hosts,*parse_allowed_hosts(os.environ.get('PROSPERO_ALLOWED_HOSTS','')))))
    config = replace(config,data_dir=root/'editing',port=port,data_dir_configured=True,base_path='/editing',allowed_hosts=allowed)
    return create_app(config,
                      resume_pending=automatic)


class Module:
    def __init__(self, name, factory, root, port, demo):
        self.name, self.factory, self.root, self.port, self.demo = name, factory, root, port, demo
        self.lock = asyncio.Lock()
        self.app = self.context = None
        self.phase = 'off'
        self.automatic = False
        self.requests = {}
        self.transition = None

    def status(self):
        return dict(id=self.name, enabled=self.phase == 'on', phase=self.phase, automatic=self.automatic,
                    active=len(self.requests), url=f'/{self.name}/')

    async def set(self, enabled, automatic=False):
        # Cancellation of a control request must not abandon a half-open/closed owner.
        async with self.lock:
            task = asyncio.create_task(self._set(enabled, automatic))
            self.transition = task
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await join_tasks([task])
                raise
            finally:
                self.transition = None

    async def _set(self, enabled, automatic):
        if enabled and self.app is None:
            self.phase = 'starting'
            app = None
            try:
                app = await self.factory(self.root, self.port, self.demo, automatic)
                context = app.router.lifespan_context(app)
                await context.__aenter__()
                self.app, self.context, self.automatic = app, context, bool(automatic)
                self.phase = 'on'
            except BaseException:
                if app is not None and hasattr(app.state, 'shutdown'):
                    await asyncio.to_thread(app.state.shutdown)
                self.phase = 'off'
                raise
        elif not enabled and self.app is not None:
            self.phase = 'stopping'
            # Disconnect active response bodies, then wait for their finally blocks.
            # In-flight ordinary writes finish before resources can close.
            active = list(self.requests.items())
            for _task, disconnect in active:
                disconnect.set()
            if active:
                await asyncio.gather(*(t for t, _ in active), return_exceptions=True)
            await self.context.__aexit__(None, None, None)
            self.app = self.context = None
            self.phase, self.automatic = 'off', False
        return self.status()

    @asynccontextmanager
    async def use(self):
        async with self.lock:
            if self.phase != 'on':
                raise ValueError(f'{self.name} is OFF. Enable it explicitly in Modules.')
            task = asyncio.current_task()
            disconnect = asyncio.Event()
            self.requests[task] = disconnect
            app = self.app
        try:
            yield app, disconnect
        finally:
            self.requests.pop(task, None)

    async def request(self, scope, receive, send):
        # The child owns a distinct task: OFF can await it independently of its caller.
        async def run():
            async with self.use() as (app, disconnect):
                started = False
                async def native_receive():
                    incoming = asyncio.create_task(receive())
                    stopped = asyncio.create_task(disconnect.wait())
                    try:
                        done, _ = await asyncio.wait({incoming, stopped}, return_when=asyncio.FIRST_COMPLETED)
                        return {'type': 'http.disconnect'} if stopped in done else incoming.result()
                    finally:
                        for pending in (incoming, stopped):
                            if not pending.done():
                                pending.cancel()
                        await asyncio.gather(incoming, stopped, return_exceptions=True)
                async def native_send(message):
                    nonlocal started
                    if message['type'] == 'http.response.start':
                        started = True
                    if started and disconnect.is_set():
                        raise asyncio.CancelledError()
                    await send(message)
                from .global_model import scope as global_scope
                with global_scope():
                    await app(scope, native_receive, native_send)
        task = asyncio.create_task(run())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Disconnect the stream, but don't lose an already dispatched mutation.
            event = self.requests.get(task)
            if event:
                event.set()
            await join_tasks([task])
            raise


class Studio:
    def __init__(self, data_dir, *, port=8815, demo=False, factories=None):
        self.root, self.port = Path(data_dir).resolve(), port
        factories = factories or {'generation': generation, 'editing': editing}
        self.modules = {name: Module(name, factory, self.root, port, demo) for name, factory in factories.items()}
        self.projects = Projects(self.root)
        if 'generation' in self.modules:
            original = self.modules['generation'].factory
            async def owned_generation(*args):
                native = await original(*args)
                loop = asyncio.get_running_loop()
                native.state.editing_handoff = lambda payload, context: asyncio.run_coroutine_threadsafe(
                    self.editing_handoff(native, payload, context), loop).result()
                return native
            self.modules['generation'].factory = owned_generation
        self.catalogs = catalog()
        self.copilot = None
        self.tools = {entry['name']: name for name, entries in self.catalogs.items() for entry in entries}
        if len(self.tools) != sum(map(len, self.catalogs.values())):
            raise ValueError('Native tool names collide; an explicit alias is required.')

    def token(self):
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / 'mcp-token'
        if path.is_file() and path.read_text(encoding='utf-8').strip():
            return path.read_text(encoding='utf-8').strip()
        from .hoard_link.atomic import write_text_atomic
        value = secrets.token_hex(32)
        write_text_atomic(path, value)
        return value

    async def close(self):
        await asyncio.gather(*(module.set(False) for module in self.modules.values()))

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            while True:
                message = await receive()
                if message['type'] == 'lifespan.startup':
                    await asyncio.to_thread(self.token)
                    await send({'type': 'lifespan.startup.complete'})
                elif message['type'] == 'lifespan.shutdown':
                    await self.close()
                    await send({'type': 'lifespan.shutdown.complete'})
                    return
        elif scope['type'] == 'http':
            try:
                await self.http(scope, receive, send)
            except ValueError as error:
                await self.json(send, {'error': str(error)}, 409)

    @staticmethod
    async def json(send, body, status=200):
        raw = json.dumps(body, ensure_ascii=False).encode()
        await send({'type': 'http.response.start', 'status': status,
                    'headers': [(b'content-type', b'application/json; charset=utf-8')]})
        await send({'type': 'http.response.body', 'body': raw})

    @staticmethod
    async def body(receive):
        parts = []
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                raise asyncio.CancelledError()
            parts.append(message.get('body', b''))
            if not message.get('more_body'):
                return json.loads(b''.join(parts) or b'{}')

    async def http(self, scope, receive, send):
        path, method = scope['path'], scope['method']
        if path.startswith('/api/copilot/'):
            if self.copilot is None:
                from .studio_ui.api import create_app as family_app
                self.copilot = family_app(self.root)
            return await self.copilot(scope, receive, send)
        if path == '/api/studio/objects' and method == 'GET':
            return await self.json(send, await self.object_catalog())
        if path.startswith('/api/agent/') and path.removeprefix('/api/agent/') in HOST_TOOLS:
            name = path.removeprefix('/api/agent/')
            body = await self.body(receive) if method == 'POST' else {}
            return await self.json(send, await self.host_tool(name, body))
        if path in ('/api/agent/tools', '/api/studio/catalog') and method == 'GET':
            return await self.json(send, {'app': 'prospero', 'tools': sum(self.catalogs.values(), [])})
        if path == '/api/studio/modules' and method == 'GET':
            return await self.json(send, {'modules': [m.status() for m in self.modules.values()]})
        if path.startswith('/api/studio/modules/') and method == 'PUT':
            name = path.rsplit('/', 1)[1]
            body = await self.body(receive)
            if name not in self.modules or not isinstance(body.get('enabled'), bool) or not isinstance(body.get('automatic',False), bool):
                return await self.json(send, {'error':'Choose a module and boolean enabled value.'}, 400)
            self.token()
            return await self.json(send, await self.modules[name].set(bool(body.get('enabled')), bool(body.get('automatic'))))
        if path == '/api/studio/projects':
            result = await asyncio.to_thread(self.projects.create, (await self.body(receive)).get('title')) if method == 'POST' else await asyncio.to_thread(self.projects.list)
            return await self.json(send, result)
        if path.startswith('/api/studio/projects/'):
            parts = path.split('/')
            project = parts[4]
            if len(parts) == 5 and method == 'GET':
                return await self.json(send, await asyncio.to_thread(self.projects.get, project))
            if len(parts) == 6 and parts[5] == 'transfer' and method == 'POST':
                body = await self.body(receive)
                async with self.modules['generation'].use() as (source, _), self.modules['editing'].use() as (target, _):
                    result = await asyncio.to_thread(self.transfer, project, source, target, body)
                return await self.json(send, result)
        if path.startswith('/api/studio/artifacts/') and method == 'GET':
            parts = path.split('/')
            if len(parts) != 6:
                return await self.json(send, {'error':'not_found'}, 404)
            project = await asyncio.to_thread(self.projects.get, parts[4])
            receipt = next((t['receipt'] for t in project['transfers'] if t['receipt']['sha256'] == parts[5]), None)
            if receipt is None:
                return await self.json(send, {'error':'not_found'}, 404)
            from starlette.responses import FileResponse
            return await FileResponse(receipt['file'], media_type='application/xml',filename='timeline.xml')(scope,receive,send)
        if path == '/api/agent/call' and method == 'POST':
            body = await self.body(receive)
            name = str(body.get('tool') or body.get('name') or '')
            if name in HOST_TOOLS:
                return await self.host_call(name, body, scope, send)
            module = self.tools.get(name)
            if not module:
                return await self.json(send, {'error': 'unknown_tool'}, 404)
            # Native routers retain schemas, reasons, journal and per-agent token semantics.
            if 'tool' in body and 'name' not in body:
                body['name'] = body.pop('tool')
            headers = dict(scope.get('headers', []))
            if module == 'editing' and self.modules[module].app is not None:
                given = headers.get(b'authorization', b'').decode()
                token_path = self.root / 'mcp-token'
                owner = token_path.read_text(encoding='utf-8').strip() if token_path.is_file() else ''
                if owner and secrets.compare_digest(given, 'Bearer ' + owner):
                    native_token = self.modules[module].app.state.services.token
                    scope = dict(scope, headers=[(k,v) for k,v in scope['headers'] if k != b'authorization'] +
                        [(b'authorization', ('Bearer '+native_token).encode())])
            raw = json.dumps(body).encode()
            sent = False
            async def replay():
                nonlocal sent
                if not sent:
                    sent = True
                    return {'type': 'http.request', 'body': raw}
                return await receive()
            return await self.native(module, scope, replay, send, '/api/agent/call')
        for module in self.modules:
            prefix = '/' + module
            if path == prefix or path.startswith(prefix + '/'):
                if path in (prefix, prefix + '/') and method == 'GET' and self.modules[module].phase == 'off':
                    title = 'Generación' if module == 'generation' else 'Montaje'
                    html = f'''<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Prospero · {title}</title><link rel="stylesheet" href="/studio-assets/studio.css"><script src="/studio-assets/off.js" defer></script></head><body class="studio-home" data-module="{module}"><header><img src="/studio-assets/app-icon.png" alt=""><h1>{title}</h1></header><main><p>Este módulo está apagado. Actívalo para abrir su editor y continuar el trabajo.</p><button id="enable">Activar {title}</button> <a href="/">Proyectos y módulos</a><p id="error" role="alert"></p></main></body></html>'''
                    await send({'type':'http.response.start','status':200,'headers':[(b'content-type',b'text/html; charset=utf-8')]})
                    return await send({'type':'http.response.body','body':html.encode()})
                return await self.native(module, scope, receive, send, path[len(prefix):] or '/')
        if path == '/api/health' and method == 'GET':
            return await self.json(send, {'service': 'prosperos-hoard', 'modules': [m.status() for m in self.modules.values()]})
        if path == '/' or path.startswith('/studio-assets/'):
            filename = 'ui.html' if path == '/' else path.removeprefix('/studio-assets/')
            font = filename.startswith('fonts/') and len(Path(filename).parts) == 2 and Path(filename).suffix in {'.woff2','.txt'}
            if filename not in {'ui.html', 'studio.js', 'studio.css', 'native.js', 'off.js', 'app-icon.png'} and not font:
                return await self.json(send, {'error': 'not_found'}, 404)
            file = app_icon() if filename == 'app-icon.png' else Path(__file__).with_name('studio_ui') / filename
            from mimetypes import guess_type
            await send({'type': 'http.response.start', 'status': 200, 'headers': [(b'content-type', (guess_type(file)[0] or 'application/octet-stream').encode())]})
            return await send({'type': 'http.response.body', 'body': await asyncio.to_thread(file.read_bytes)})
        # Old generation paths remain callable when generation is explicitly ON.
        return await self.native('generation', scope, receive, send, path)

    async def object_catalog(self):
        result = {'generation': [], 'editing': []}
        if self.modules['generation'].phase == 'on':
            async with self.modules['generation'].use() as (app, _):
                def read():
                    store = app.state.store
                    return [{**p, 'timelines':store.list_timelines(p['id'])} for p in store.list_projects(limit=10000)['items']]
                result['generation'] = await asyncio.to_thread(read)
        if self.modules['editing'].phase == 'on':
            async with self.modules['editing'].use() as (app, _):
                from .editing.projects import list_projects
                result['editing'] = await asyncio.to_thread(list_projects, app.state.services)
        return result

    async def editing_handoff(self, source, payload, context):
        if self.modules['editing'].phase != 'on':
            return {'ok':False,'error':'Montaje está apagado. Actívalo en Módulos de Prospero.'}
        project = await asyncio.to_thread(self.projects.project_for_timeline, context['timeline_id'])
        if project is None:
            project = (await asyncio.to_thread(self.projects.create, payload['title']))['id']
        async with self.modules['editing'].use() as (target, _):
            shared = await asyncio.to_thread(self.transfer, project, source, target, context)
        result = dict(shared['transfers'][-1]['receipt']['result'])
        result.update(url='/editing/#/p/'+result['project_id'], studio_project=project)
        return {'ok':True,'result':result}

    async def host_call(self, name, body, scope, send):
        from .hoard_link.tokens import check_bearer, bearer_of, lookup_agent_token
        from .hoard_link.agent_journal import Journal, clean_identity, summarize_args, digest_args, result_ids, mask_text
        headers = dict(scope.get('headers', []))
        given = headers.get(b'authorization', b'').decode()
        path = self.root / 'mcp-token'
        token = path.read_text(encoding='utf-8').strip() if path.is_file() else ''
        access = None if check_bearer(given, token) else lookup_agent_token(self.root, bearer_of(given))
        if not check_bearer(given, token) and not access:
            return await self.json(send, {'error':'Invalid MCP token.','code':'unauthorized'}, 401)
        meta = next(t for t in self.catalogs['host'] if t['name'] == name)
        arguments = dict(body.get('arguments') or {})
        reason = str(body.get('reason') or arguments.pop('reason', '')).strip()
        write = not meta['annotations']['readOnlyHint']
        if write and access and (access['profile'] == 'read_only' or
                (access['profile'] == 'drafts' and not meta['annotations']['draftSafeHint'])):
            return await self.json(send, {'error':'Token profile does not allow this operation.','code':'profile_forbidden'}, 403)
        if write and not 3 <= len(reason) <= 300:
            return await self.json(send, {'error':'Give a reason (3-300 characters).','code':'reason_required'}, 400)
        result = await self.host_tool(name, arguments)
        if write:
            await asyncio.to_thread(Journal(self.root, app='prospero').append, {
                'tool':name, 'args_digest':digest_args(arguments), 'args_summary':summarize_args(arguments),
                'reason':mask_text(reason), 'ok':True, 'ids':result_ids(result),
                'agent': (access or {}).get('agent') or clean_identity(headers.get(b'x-agent-id',b'').decode()),
                'session':clean_identity(headers.get(b'x-agent-session',b'').decode(),120),
                'undoable':False, 'not_undoable_reason':'draft_safe' if meta['annotations']['draftSafeHint'] else 'no_handler'})
        return await self.json(send, result)

    async def host_tool(self, name, args):
        if name == 'audiovisual_objects':
            return await self.object_catalog()
        if name == 'studio_modules':
            return {'modules':[m.status() for m in self.modules.values()]}
        if name == 'studio_module_set':
            if args.get('module') not in self.modules or not isinstance(args.get('enabled'), bool) or not isinstance(args.get('automatic',False), bool):
                raise ValueError('Choose a module and a boolean enabled value.')
            self.token()
            return await self.modules[args['module']].set(args['enabled'], args.get('automatic', False))
        if name == 'audiovisual_projects':
            return await asyncio.to_thread(self.projects.get, args['project']) if args.get('project') else await asyncio.to_thread(self.projects.list)
        if name == 'studio_project_create':
            return await asyncio.to_thread(self.projects.create, args.get('title', ''))
        if name == 'studio_project_transfer':
            async with self.modules['generation'].use() as (source, _), self.modules['editing'].use() as (target, _):
                return await asyncio.to_thread(self.transfer, args['project'], source, target, args)
        raise ValueError('Unknown studio operation.')

    async def native(self, name, scope, receive, send, path):
        from .studio_response import NativeResponse
        # ASGI keeps the public path intact; routers subtract root_path. A
        # stripped path plus a prefixed root makes nested StaticFiles mounts
        # look for assets/assets instead of the packaged asset.
        child_path = '/' + name + path
        child = dict(scope, path=child_path, raw_path=child_path.encode(), root_path='/' + name)
        child['asgi'] = {**scope.get('asgi', {}), 'spec_version': '2.3'}
        await self.modules[name].request(child, receive, NativeResponse(send, name, self.port))

    def transfer(self, project, source, target, body):
        from . import exporters, productions
        from .editing.timeline_import import project_from_timeline
        from .editing.projects import doc
        from .hoard_link.atomic import write_text_atomic
        import hashlib
        self.projects.get(project)
        store = source.state.store
        tid = str(body.get('timeline_id') or '')
        timeline = store.get_timeline(tid)
        production = str(body.get('production') or '')
        production_state = productions.load_state(store.data_dir, production) if production else None
        if production_state and production_state.get('project_id') != timeline['project_id']:
            raise ValueError('The production and timeline belong to different projects.')
        def lookup(aid):
            asset = store.get_asset(aid)
            path = Path(asset['file_path'])
            path = path if path.is_absolute() else store.data_dir / path
            return {**asset, 'path': str(path.resolve()), 'name': asset.get('name') or aid}
        xml = exporters.to_xmeml(timeline, lookup)
        sha = hashlib.sha256(xml.encode()).hexdigest()
        folder = self.root / 'studio-artifacts' / project
        folder.mkdir(parents=True, exist_ok=True)
        xml_path = folder / (sha + '.xml')
        if not xml_path.exists():
            write_text_atomic(xml_path, xml)
        result = project_from_timeline(target.state.services, title=timeline.get('name') or tid, fcpxml_path=str(xml_path))
        target_id = result['project_id']
        asset_ids = {c.get('asset_id') for t in timeline.get('tracks', []) for c in t.get('clips', []) if c.get('asset_id')}
        if timeline.get('audio_asset_id'):
            asset_ids.add(timeline['audio_asset_id'])
        source_link = self.projects.link(project, 'generation', 'timeline', tid,
            f'/generation/#/p/{timeline["project_id"]}/timeline/{tid}',
            {'timeline': timeline, 'production': production_state, 'assets':[lookup(aid) for aid in sorted(asset_ids)]})
        target_link = self.projects.link(project, 'editing', 'project', target_id,
            f'/editing/#/p/{target_id}', {'result': result, 'project': doc(target.state.services, target_id).dump()})
        return self.projects.receipt(project, source_link['id'], target_link['id'],
            {'format': 'xmeml', 'sha256': sha, 'file': str(xml_path), 'result': result})


def create_app(data_dir, **kwargs):
    from .hoard_link.guard import GuardMiddleware, parse_allowed_hosts
    import os
    studio = Studio(data_dir, **kwargs)
    guarded = GuardMiddleware(studio, port_getter=lambda: studio.port,
        allowed=parse_allowed_hosts(os.environ.get('PROSPERO_ALLOWED_HOSTS', '')), strict_ports=True)
    guarded.studio = studio
    return guarded
