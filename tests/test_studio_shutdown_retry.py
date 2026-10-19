"""Failed native cleanup retains its owner and retries the unfinished step."""
import asyncio
from contextlib import asynccontextmanager
import socket
import sys

from fastapi import FastAPI
import httpx
import pytest

from prosperos_hoard.studio import Module, generation, editing


@pytest.mark.asyncio
async def test_failed_control_returns_json_and_retains_pending_owner(tmp_path):
    from prosperos_hoard.studio import create_app
    @asynccontextmanager
    async def lifespan(app):
        yield
        raise OSError('resource is still owned')
    native = FastAPI(lifespan=lifespan)
    async def factory(*args):
        return native
    app = create_app(tmp_path, port=64839)
    app.studio.modules['editing'].factory = factory
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://127.0.0.1:64839') as client:
        assert (await client.put('/api/studio/modules/editing', json={'enabled': True})).status_code == 200
        response = await client.put('/api/studio/modules/editing', json={'enabled': False})
        assert response.status_code == 503
        assert response.json()['error'] == 'resource is still owned'
        assert response.json()['module']['phase'] == 'stopping'
        assert app.studio.modules['editing'].app is native


@pytest.mark.asyncio
async def test_unknown_failed_lifespan_stays_owned(tmp_path):
    @asynccontextmanager
    async def lifespan(app):
        yield
        raise OSError('cleanup unavailable')
    app = FastAPI(lifespan=lifespan)
    async def factory(*args):
        return app
    owner = Module('synthetic', factory, tmp_path, 64839, False)
    await owner.set(True)
    with pytest.raises(OSError):
        await owner.set(False)
    with pytest.raises(RuntimeError, match='no retry callback'):
        await owner.set(False)
    with pytest.raises(RuntimeError, match='shutdown before enabling'):
        await owner.set(True)
    assert owner.app is app and owner.phase == 'stopping'


@pytest.mark.asyncio
@pytest.mark.parametrize('name,factory', [('generation', generation), ('editing', editing)])
async def test_real_native_factory_retries_twice_then_closes(name, factory, tmp_path, monkeypatch):
    from prosperos_hoard.hoard_link import family as generation_family
    from prosperos_hoard.editing.hoard_link import family as editing_family
    from prosperos_hoard.editing.services import Services
    monkeypatch.setattr(generation_family, 'emit', lambda *a, **k: False)
    monkeypatch.setattr(editing_family, 'emit', lambda *a, **k: False)
    monkeypatch.setattr(Services, '_build_link', lambda self: None)
    connect = socket.socket.connect
    pair_code = getattr(socket.socketpair, '__code__', None)
    def offline(sock, *args, **kwargs):
        frame = sys._getframe(1)
        while frame:
            if frame.f_code is pair_code:
                return connect(sock, *args, **kwargs)
            frame = frame.f_back
        raise OSError('No outbound service in synthetic lifecycle QA')
    monkeypatch.setattr(socket.socket, 'connect', offline)
    owner = Module(name, factory, tmp_path, 64839, False)
    await owner.set(True)
    app = owner.app
    target = app.state.backend if name == 'generation' else app.state.services.notifier
    method = 'close'
    original = getattr(target, method)
    calls = []
    def temporary_failure(*args, **kwargs):
        calls.append('close')
        if len(calls) < 3:
            raise OSError('Synthetic temporary cleanup failure')
        return original(*args, **kwargs)
    monkeypatch.setattr(target, method, temporary_failure)
    try:
        for count in (1, 2):
            with pytest.raises(OSError):
                await owner.set(False)
            assert len(calls) == count
            assert owner.app is app and owner.phase == 'stopping'
            assert not owner.status()['enabled']
            with pytest.raises(RuntimeError, match='shutdown before enabling'):
                await owner.set(True)
        await owner.set(False)
        assert len(calls) == 3
        assert owner.app is None and owner.phase == 'off'
    finally:
        if owner.app is not None:
            monkeypatch.setattr(target, method, original)
            await app.state.retry_shutdown()
