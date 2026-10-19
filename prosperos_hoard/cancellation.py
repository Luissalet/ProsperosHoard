"""Join owned cleanup through repeated asyncio and AnyIO cancellation.

Adapted from the independently corrected publishing owner (b818 series).
This module is owned here and has no dependency on another checkout.
"""
import asyncio
import anyio


async def join_tasks(tasks):
    joined = asyncio.gather(*dict.fromkeys(tasks), return_exceptions=True)
    interrupted = False
    with anyio.CancelScope(shield=True):
        while True:
            try:
                result = await asyncio.shield(joined)
                break
            except asyncio.CancelledError:
                interrupted = True
    if interrupted:
        raise asyncio.CancelledError
    return result
