"""Causal CAS1 review reproductions, no listener/model/native executable."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import json

import anyio
import pytest

from prosperos_hoard.studio import Module
from prosperos_hoard.studio_response import NativeResponse, scoped_json


def native_fixture():
    started, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))
    closed = []
    @asynccontextmanager
    async def lifespan(app):
        yield
        closed.append(cleaned.is_set())
    class Native:
        router = SimpleNamespace(lifespan_context=lifespan)
        async def __call__(self, scope, receive, send):
            try:
                await send({'type':'http.response.start','status':200,'headers':[]})
                started.set()
                await receive()
            finally:
                cleaning.set()
                await release.wait()
                cleaned.set()
    async def factory(*args): return Native()
    return factory, started, cleaning, release, cleaned, closed


async def receive(): await asyncio.Future()
async def send(_message): pass


@pytest.mark.asyncio
async def test_repeated_consumer_cancel_keeps_off_pending_until_native_finally(tmp_path):
    factory, started, cleaning, release, cleaned, closed = native_fixture()
    module = Module('editing', factory, tmp_path, 8815, False)
    await module.set(True)
    caller = asyncio.create_task(module.request({'type':'http'}, receive, send))
    await started.wait()
    caller.cancel()
    await cleaning.wait()
    caller.cancel()
    stopping = asyncio.create_task(module.set(False))
    async with asyncio.timeout(2):
        while module.phase != 'stopping':
            await asyncio.sleep(0)
    assert not caller.done() and not stopping.done() and not closed and not cleaned.is_set()
    assert module.requests
    other = Module('generation', None, tmp_path, 8815, False)
    assert (await other.set(False))['phase'] == 'off'
    release.set()
    await asyncio.gather(caller, return_exceptions=True)
    await stopping
    assert closed == [True] and module.phase == 'off' and not module.requests


@pytest.mark.asyncio
@pytest.mark.parametrize('enable', [True, False])
async def test_repeated_transition_cancel_joins_its_lifespan(tmp_path, enable):
    entered, release, completed = (asyncio.Event() for _ in range(3))
    @asynccontextmanager
    async def lifetime(app):
        if enable:
            entered.set()
            await release.wait()
            completed.set()
        yield
        if not enable:
            entered.set()
            await release.wait()
            completed.set()
    native = SimpleNamespace(router=SimpleNamespace(lifespan_context=lifetime))
    async def factory(*_args): return native
    module = Module('editing', factory, tmp_path, 8815, False)
    if not enable: await module.set(True)
    caller = asyncio.create_task(module.set(enable))
    await entered.wait()
    caller.cancel()
    await asyncio.sleep(0)
    caller.cancel()
    await asyncio.sleep(0)
    assert not caller.done() and not completed.is_set() and module.transition is not None
    release.set()
    await asyncio.gather(caller, return_exceptions=True)
    assert completed.is_set() and module.transition is None
    assert module.phase == ('on' if enable else 'off')
    if enable: await module.set(False)


@pytest.mark.asyncio
async def test_anyio_scope_cancellation_does_not_abandon_cleanup(tmp_path):
    factory, started, cleaning, release, cleaned, closed = native_fixture()
    module = Module('editing', factory, tmp_path, 8815, False)
    await module.set(True)
    finished = asyncio.Event()
    async with anyio.create_task_group() as group:
        async def consume(*, task_status=anyio.TASK_STATUS_IGNORED):
            with anyio.CancelScope() as cancellation:
                task_status.started(cancellation)
                await module.request({'type':'http'}, receive, send)
            finished.set()
        cancellation = await group.start(consume)
        await started.wait()
        cancellation.cancel()
        await cleaning.wait()
        cancellation.cancel()
        stopping = asyncio.create_task(module.set(False))
        async with asyncio.timeout(2):
            while module.phase != 'stopping':
                await asyncio.sleep(0)
        assert not finished.is_set() and not stopping.done() and not closed
        release.set()
        await stopping
    assert finished.is_set() and cleaned.is_set() and closed == [True]


@pytest.mark.asyncio
async def test_json_scopes_known_links_only_and_keeps_native_content_exact():
    native = {'title':'/api/title-that-user-typed','prompt':'/assets/user-provided-text',
        'file_path':'/assets/original.wav','path':'/api/source.png','id':'/api/native-id',
        'snapshot':{'url':'/api/original','title':'/assets/exact'},
        'doc':{'url':'/api/original','tracks':[{'src':'/assets/original'}]},
        'manifest':{'download_url':'/api/original'}, 'params':{'url':'/api/source'},
        'media':{'m1':{'urls':{'play':'/api/media/m1/play','file':'http://localhost:8815/api/media/m1/file',
            'poster':'/editing/api/media/m1/poster','label':'/assets/exact'}}},
        'thumbnail':'/api/thumbnail','download_url':'http://127.0.0.1:8815/api/export/a',
        'items':[{'preview_url':'/api/preview','title':'/api/exact'}]}
    result = scoped_json(native, 'editing', 8815)
    for key in ('title','prompt','file_path','path','id','snapshot','doc','manifest','params'):
        assert result[key] == native[key]
    assert result['media']['m1']['urls'] == {'play':'/editing/api/media/m1/play',
        'file':'/editing/api/media/m1/file','poster':'/editing/api/media/m1/poster','label':'/assets/exact'}
    assert result['download_url'] == '/editing/api/export/a'
    assert result['items'][0] == {'preview_url':'/editing/api/preview','title':'/api/exact'}
    assert scoped_json(result, 'editing', 8815) == result
    messages = []
    async def collect(message): messages.append(message)
    response = NativeResponse(collect, 'editing', 8815)
    await response({'type':'http.response.start','status':200,'headers':[(b'content-type',b'application/json')]})
    await response({'type':'http.response.body','body':json.dumps(native).encode()})
    assert json.loads(messages[-1]['body']) == result
