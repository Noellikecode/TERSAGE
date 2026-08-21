"""Time and identity generation.

Nothing in FIRST DUE reads the wall clock directly. Time arrives through a
:class:`Clock` so that a replayed incident produces byte-identical output, and
identifiers arrive through an :class:`IdGenerator` for the same reason.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Source of timezone-aware time."""

    def now(self) -> datetime:
        """Return the current time. Always timezone-aware."""
        ...


@runtime_checkable
class IdGenerator(Protocol):
    """Source of identifiers and idempotency keys."""

    def new_id(self, prefix: str) -> str:
        """Return a new identifier namespaced by ``prefix`` (e.g. ``fact``)."""
        ...

    def idempotency_key(self, *parts: str) -> str:
        """Derive a stable idempotency key from its natural key parts.

        Deriving rather than randomising is the point: the same logical write,
        retried after a crash or redelivered by Pub/Sub, produces the same key
        and therefore cannot execute twice.
        """
        ...
