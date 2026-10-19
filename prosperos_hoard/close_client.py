"""Checkpoint owned HTTP resources instead of HTTPX's early CLOSED flag."""
import asyncio
from contextlib import nullcontext
import httpx


class ClosePlan:
    def __init__(self, client):
        self.client, self.completed = client, 0
        self.steps = []
        self.lock = asyncio.Lock()
        by_id = {}

        def add(resource, pool=None):
            step = by_id.get(id(resource))
            if step is None:
                step = [resource, []]
                by_id[id(resource)] = step
                self.steps.append(step)
            if pool is not None and all(pool is not owner for owner in step[1]):
                step[1].append(pool)

        for transport in (client._transport, *client._mounts.values()):
            if transport is None:
                continue
            pool = getattr(transport, '_pool', None)
            for connection in list(getattr(pool, '_connections', ())):
                add(connection, pool)
            add(transport)

    async def close(self):
        async with self.lock:
            while self.completed < len(self.steps):
                resource, pools = self.steps[self.completed]
                await resource.aclose()
                # Pools otherwise consume/re-close these connections before
                # awaiting them. Remove only successfully closed connections.
                for pool in pools:
                    with getattr(pool, '_optional_thread_lock', nullcontext()):
                        pool._connections = [c for c in pool._connections if c is not resource]
                self.completed += 1
            self.client._state = type(self.client._state).CLOSED


async def close_client(client):
    if not isinstance(client, httpx.AsyncClient):
        nested = getattr(client, '_client', None)
        if isinstance(nested, httpx.AsyncClient):
            if client._owns_client:
                await close_client(nested)
            return
        return await client.aclose()

    async def drain():
        plan = getattr(client, '_hoard_close_plan', None)
        if plan is None:
            plan = client._hoard_close_plan = ClosePlan(client)
        await plan.close()

    loop = getattr(client, '_hoard_owner_loop', None)
    if loop is not None and loop is not asyncio.get_running_loop() and loop.is_running():
        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(drain(), loop))
    else:
        await drain()


class OwnedAsyncClient(httpx.AsyncClient):
    async def send(self, *args, **kwargs):
        self._hoard_owner_loop = asyncio.get_running_loop()
        return await super().send(*args, **kwargs)

    async def aclose(self):
        await close_client(self)
