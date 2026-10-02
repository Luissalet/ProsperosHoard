"""Ids: ULIDs that stay ordered inside one millisecond (the job queue relies on it), prefixed as ``<prefix>_<ULID>``. Hoard Link's
``hoard_link.ids``; the names stay here for the modules that already import them."""

from __future__ import annotations

from .hoard_link.ids import new_id, new_ulid  # noqa: F401 - re-exported
