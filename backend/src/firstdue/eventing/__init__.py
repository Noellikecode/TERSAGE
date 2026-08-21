"""Event delivery mechanics shared by every transport.

The in-memory bus and the Pub/Sub push endpoint are two transports over one
delivery policy. Putting the policy here rather than in either adapter is what
makes the credential-free demo a faithful rehearsal of the deployed system:
dedupe, retry classification, backoff, circuit breaking, and dead-lettering are
the same code on both paths.
"""

from __future__ import annotations

from firstdue.eventing.deadletter import DeadLetterRecord, InMemoryDeadLetterStore
from firstdue.eventing.dispatch import (
    DeliveryOutcome,
    DeliveryStatus,
    EventDispatcher,
    MemoryDedupeStore,
    RepositoryDedupeStore,
    Subscription,
    VirtualSleeper,
    route,
)

__all__ = [
    "DeadLetterRecord",
    "DeliveryOutcome",
    "DeliveryStatus",
    "EventDispatcher",
    "InMemoryDeadLetterStore",
    "MemoryDedupeStore",
    "RepositoryDedupeStore",
    "Subscription",
    "VirtualSleeper",
    "route",
]
