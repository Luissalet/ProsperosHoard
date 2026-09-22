"""Small, dependency-free ULID-like id generator.

Not a byte-perfect ULID implementation, but it has the properties this app
needs: lexicographically sortable by creation time, URL-safe, and collision
resistant enough for a single-user local app. Format: 10 base32 chars of
millisecond timestamp + 16 base32 chars of randomness.
"""

from __future__ import annotations

import os
import threading
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode(number: int, length: int) -> str:
    chars = []
    for _ in range(length):
        number, rem = divmod(number, 32)
        chars.append(_CROCKFORD[rem])
    return "".join(reversed(chars))


_lock = threading.Lock()
_last = {"ts": -1, "rand": 0}


def new_ulid() -> str:
    """Monotonic within the process: two ids made in the same millisecond
    still sort in creation order (the job queue relies on it)."""
    with _lock:
        ts_ms = int(time.time() * 1000)
        if ts_ms <= _last["ts"]:
            ts_ms = _last["ts"]
            rand = _last["rand"] + 1
        else:
            rand = int.from_bytes(os.urandom(10), "big") >> 1  # headroom for increments
        _last["ts"], _last["rand"] = ts_ms, rand
    return _encode(ts_ms, 10) + _encode(rand, 16)


def new_id(prefix: str) -> str:
    return f"{prefix}_{new_ulid()}"
