"""Event bus protocol.

Agents never call each other. Every handoff is a published envelope carrying
identifiers; every consumer re-reads state from the store, so a replayed event
produces the same result as the original delivery.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol, runtime_checkable

from firstdue.domain.events import EventEnvelope, Topic

EventHandler = Callable[[EventEnvelope], Awaitable[None]]


@runtime_checkable
class EventBus(Protocol):
    async def publish(self, envelope: EventEnvelope) -> None:
        """Publish an envelope to its topic.

        Delivery is at-least-once. Consumers must be idempotent on
        ``envelope.idempotency_key``.
        """
        ...

    def subscribe(self, topic: Topic, handler: EventHandler, *, subscriber: str) -> None:
        """Register a handler. ``subscriber`` names the consumer for dedupe."""
        ...

    async def dead_letters(self) -> Sequence[EventEnvelope]:
        """Envelopes that exhausted their retries -- surfaced, never dropped."""
        ...
