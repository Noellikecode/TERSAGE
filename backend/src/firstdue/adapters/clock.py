"""Clock and identifier adapters.

The deterministic implementations exist so a demo reset, a test run, and a NIOSH
replay all produce the same identifiers and the same timestamps. Nothing in the
system reads ``datetime.now()`` directly.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Final

#: Namespace for deterministic identifier derivation.
_NAMESPACE: Final[uuid.UUID] = uuid.UUID("6f1d5b2e-0d3a-4f6b-9a5e-6c0a2b8f1d44")


class SystemClock:
    """Wall clock, always timezone-aware."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """A clock frozen at one instant. Used by tests that assert on timestamps."""

    def __init__(self, instant: datetime) -> None:
        if instant.tzinfo is None:
            raise ValueError("FixedClock requires a timezone-aware instant")
        self._instant = instant

    def now(self) -> datetime:
        return self._instant

    def set(self, instant: datetime) -> None:
        self._instant = instant

    def advance(self, delta: timedelta) -> None:
        self._instant = self._instant + delta


class SteppingClock:
    """Advances a fixed step on every read.

    Gives the demo a monotonic, reproducible timeline: run the seed twice and
    every timestamp matches.
    """

    def __init__(self, start: datetime, *, step: timedelta = timedelta(milliseconds=50)) -> None:
        if start.tzinfo is None:
            raise ValueError("SteppingClock requires a timezone-aware start")
        self._current = start
        self._step = step

    def now(self) -> datetime:
        current = self._current
        self._current = current + self._step
        return current

    def peek(self) -> datetime:
        return self._current

    def advance(self, delta: timedelta) -> None:
        self._current = self._current + delta


class DeterministicIdGenerator:
    """Seeded identifiers: same seed and same call order, same ids, forever."""

    def __init__(self, seed: str = "firstdue") -> None:
        self._seed = seed
        self._counters: dict[str, int] = {}

    def new_id(self, prefix: str) -> str:
        if not prefix or not prefix.isidentifier():
            raise ValueError("id prefix must be a simple identifier")
        count = self._counters.get(prefix, 0)
        self._counters[prefix] = count + 1
        token = uuid.uuid5(_NAMESPACE, f"{self._seed}:{prefix}:{count}").hex[:12]
        return f"{prefix}_{token}"

    def idempotency_key(self, *parts: str) -> str:
        """Derive a stable key from the write's natural key.

        Derived, not random: the same logical write retried after a crash or
        redelivered by Pub/Sub produces the same key and cannot execute twice.
        """
        if not parts:
            raise ValueError("an idempotency key needs at least one natural key part")
        material = "|".join(parts)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

    def reset(self) -> None:
        self._counters.clear()


class RandomIdGenerator:
    """Non-deterministic ids for live mode, where replay uses stored ids."""

    def new_id(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:12]}"

    def idempotency_key(self, *parts: str) -> str:
        if not parts:
            raise ValueError("an idempotency key needs at least one natural key part")
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]
