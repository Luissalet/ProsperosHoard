"""Small, dependency-free ULID-like id generator.

Not a byte-perfect ULID implementation, but it has the properties this app
needs: lexicographically sortable by creation time, URL-safe, and collision
resistant enough for a single-user local app. Format: 10 base32 chars of
millisecond timestamp + 16 base32 chars of randomness.
"""

from __future__ import annotations

import os
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode(number: int, length: int) -> str:
    chars = []
    for _ in range(length):
        number, rem = divmod(number, 32)
        chars.append(_CROCKFORD[rem])
    return "".join(reversed(chars))


def new_ulid() -> str:
    ts_ms = int(time.time() * 1000)
    rand = int.from_bytes(os.urandom(10), "big")
    return _encode(ts_ms, 10) + _encode(rand, 16)


def new_id(prefix: str) -> str:
    return f"{prefix}_{new_ulid()}"
