"""Real transport failures must remain retryable through native OFF."""
PRODUCT = 'prospero'
from prosperos_hoard.close_client import close_client, OwnedAsyncClient

import asyncio
from contextlib import asynccontextmanager
from functools import wraps
import importlib
import socket
import sys
import threading
import httpx
import httpcore
import pytest
from httpcore._async.http11 import AsyncHTTP11Connection

def run_async(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run

async def endpoint(scope, receive, send):
    await send({'type':'http.response.start','status':200,'headers':[]})
    await send({'type':'http.response.body','body':b'{}'})

class Transport(httpx.ASGITransport):
    def __init__(self, failures=0, hold=False):
        super().__init__(app=endpoint)
        self.attempts, self.failures, self.open = 0, failures, True
        self.hold = hold
        self.entered, self.release = threading.Event(), threading.Event()
    async def aclose(self):
        self.attempts += 1
        if self.attempts <= self.failures:
            raise OSError('Transport remains owned')
        if self.hold:
            self.entered.set()
            while not self.release.is_set():
                await asyncio.sleep(.005)
        await super().aclose()
        self.open = False

@pytest.fixture(autouse=True)
def no_external_sockets(monkeypatch):
    monkeypatch.setenv('HOARD_EVENTS','0')
    original, pair = socket.socket.connect, getattr(socket.socketpair,'__code__',None)
    def guarded(*args, **kwargs):
        frame = sys._getframe(1)
        while frame:
            if frame.f_code is pair:
                return original(*args, **kwargs)
            frame = frame.f_back
        raise AssertionError('No external sockets in transport lifecycle QA')
    monkeypatch.setattr(socket.socket,'connect',guarded)

@run_async
async def test_unique_default_mounts_checkpoint_successful_transport():
    first, last = Transport(), Transport(2)
    client = OwnedAsyncClient(transport=first, mounts={'http://proxy.invalid':last,'http://duplicate.invalid':first},trust_env=False)
    for count in (1,2):
        with pytest.raises(OSError):
            await close_client(client)
        assert first.attempts == 1 and last.attempts == count
        assert not client.is_closed
    await close_client(client)
    assert not first.open and not last.open and client.is_closed
    await close_client(client)
    assert first.attempts == 1 and last.attempts == 3

@run_async
async def test_recover_httpx_already_closed_flag_without_skipping_transport():
    transport = Transport(2)
    client = httpx.AsyncClient(transport=transport,trust_env=False)
    with pytest.raises(OSError):
        await client.aclose()
    assert client.is_closed and transport.open and transport.attempts == 1
    with pytest.raises(OSError):
        await close_client(client)
    assert transport.attempts == 2 and transport.open
    await close_client(client)
    assert transport.attempts == 3 and not transport.open

@run_async
async def test_real_httpcore_pool_preserves_failed_connection_and_skips_closed_ones():
    class Stream:
        def __init__(self, failures=0):
            self.attempts, self.failures, self.open = 0, failures, True
        async def aclose(self):
            self.attempts += 1
            if self.attempts <= self.failures:
                raise OSError('Network stream remains owned')
            self.open = False
    transport = httpx.AsyncHTTPTransport()
    origin = httpcore.Origin(b'http', b'synthetic.invalid', 80)
    streams = [Stream(), Stream(2)]
    connections = []
    for stream in streams:
        connection = httpcore.AsyncHTTPConnection(origin)
        connection._connection = AsyncHTTP11Connection(origin, stream)
        connections.append(connection)
    transport._pool._connections = connections[:]
    client = OwnedAsyncClient(transport=transport,trust_env=False)
    for count in (1,2):
        with pytest.raises(OSError):
            await close_client(client)
        assert streams[0].attempts == 1 and not streams[0].open
        assert streams[1].attempts == count and streams[1].open
        assert transport._pool._connections == [connections[1]]
        assert not client.is_closed
    await close_client(client)
    assert [s.attempts for s in streams] == [1,3]
    assert not any(s.open for s in streams)
    assert client.is_closed and not transport._pool._connections

async def native_owner(tmp_path, kind):
    if PRODUCT == 'vulcan':
        from vulcan.studio.app import Module
        from vulcan.studio.native import create_native
        owner = Module({'id':'library'},tmp_path,lambda k,p,a:create_native(k,p,port=64836,automatic=False))
        await owner.set_enabled(True)
        app, link = owner.app, owner.app.state.services.link
        return owner, owner.set_enabled, app, link, link._client if kind == 'parent' else None
    from prosperos_hoard.studio import Module, generation, editing
    mode = 'editing' if kind == 'editing-parent' else 'generation'
    owner = Module(mode,editing if mode == 'editing' else generation,tmp_path,64839,False)
    await owner.set(True)
    app = owner.app
    link = app.state.services._link if mode == 'editing' else app.state.backend.link
    if 'pool' in kind:
        resource = await asyncio.to_thread(app.state.backend._pool_client,'http://synthetic.invalid')
        client = resource._client
    elif 'parent' in kind:
        client = link._client
    else:
        client = None
    return owner, owner.set, app, link, client

async def attach_transport(app, link, client, kind, transport):
    parent = 'parent' in kind
    if client is None:
        await asyncio.to_thread(link.sync._run, lambda _:asyncio.sleep(0))
        client = link.sync._inner._client
    if parent:
        await client._transport.aclose()
        client._transport = transport
        assert (await client.get('http://synthetic.invalid')).status_code == 200
    else:
        await asyncio.to_thread(link.sync._run, lambda _:client._transport.aclose())
        client._transport = transport
        assert (await asyncio.to_thread(link.sync._run, lambda _:client.get('http://synthetic.invalid'))).status_code == 200
    if kind.startswith('retired-'):
        await asyncio.to_thread(app.state.backend.reload)
    return client

KINDS = ['inner','parent'] if PRODUCT == 'vulcan' else ['inner','parent','pool','retired-inner','retired-parent','retired-pool','editing-parent']

@run_async
@pytest.mark.parametrize('kind',KINDS)
async def test_native_factory_retries_actual_transport_and_keeps_owner(kind,tmp_path):
    owner, change, app, link, client = await native_owner(tmp_path,kind)
    transport = Transport(2)
    client = await attach_transport(app,link,client,kind,transport)
    try:
        for count in (1,2):
            with pytest.raises(OSError):
                await change(False)
            assert transport.attempts == count and transport.open
            assert owner.app is app and owner.status()['phase'] == 'stopping'
            with pytest.raises(RuntimeError):
                await change(True)
        await change(False)
        assert transport.attempts == 3 and not transport.open and client.is_closed
        assert owner.app is None and owner.status()['phase'] == 'off'
        assert link.sync._loop is None and link.sync._thread is None
    finally:
        transport.release.set()
        if owner.app is not None:
            transport.failures = 0
            await app.state.retry_shutdown()

@run_async
async def test_third_transport_close_retains_owner_under_two_cancellations(tmp_path):
    kind = 'inner' if PRODUCT == 'vulcan' else 'retired-pool'
    owner, change, app, link, client = await native_owner(tmp_path,kind)
    transport = Transport(2, hold=True)
    client = await attach_transport(app,link,client,kind,transport)
    try:
        for _ in range(2):
            with pytest.raises(OSError):
                await change(False)
        closing = asyncio.create_task(change(False))
        assert await asyncio.to_thread(transport.entered.wait,2)
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done() and owner.app is app and transport.open
        transport.release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert transport.attempts == 3 and not transport.open and client.is_closed
        assert owner.app is None and owner.status()['phase'] == 'off'
    finally:
        transport.release.set()
        if owner.app is not None:
            transport.failures = 0
            await app.state.retry_shutdown()
