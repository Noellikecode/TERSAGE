"""In-memory event bus with the delivery semantics Pub/Sub actually has.

Same as the live path, deliberately -- and now literally: both transports run
:class:`~firstdue.eventing.dispatch.EventDispatcher`, so what fake mode proves
is what the deployed system does.

* **at-least-once delivery** -- consumers dedupe on ``idempotency_key``, scoped
  per subscriber so two consumers of a topic each get their turn;
* **deterministic ordering** -- envelopes are delivered in publish order, and
  subscribers in registration order, so a replay is reproducible;
* **classified failures** -- transient failures retry with derived jittered
  backoff; poison and permanent ones dead-letter on the first attempt rather
  than burning a retry budget on a message that cannot succeed;
* **bounded retries then a dead letter** -- surfaced, never silently dropped;
* **no payloads** -- the envelope already refuses anything but identifiers.

Backoff is computed and recorded but not slept through: fake mode is
deterministic, and on the live path the waiting belongs to Pub/Sub's redelivery
rather than to this process. Pass a real sleeper to change that.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from firstdue.adapters.clock import SystemClock
from firstdue.domain.events import EventEnvelope, Topic
from firstdue.eventing.deadletter import DeadLetterRecord, InMemoryDeadLetterStore
from firstdue.eventing.dispatch import (
    DedupeStore,
    DeliveryOutcome,
    EventDispatcher,
    MemoryDedupeStore,
    Sleeper,
    Subscription,
    route,
)
from firstdue.ports.bus import EventHandler
from firstdue.ports.clock import Clock
from firstdue.reliability.retry import DEFAULT_POLICY, RetryPolicy

#: Kept as the historical name for the dead-letter record.
DeadLetter = DeadLetterRecord


class InMemoryEventBus:
    """Deterministic, in-process bus."""

    def __init__(
        self,
        *,
        max_attempts: int = 3,
        clock: Clock | None = None,
        policy: RetryPolicy | None = None,
        dedupe: DedupeStore | None = None,
        sleeper: Sleeper | None = None,
        dead_letters: InMemoryDeadLetterStore | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        resolved_policy = policy or DEFAULT_POLICY.model_copy(update={"max_attempts": max_attempts})
        self._subscriptions: dict[Topic, list[Subscription]] = {}
        self._published: list[EventEnvelope] = []
        self._outcomes: list[DeliveryOutcome] = []
        self._dead_letter_store = dead_letters or InMemoryDeadLetterStore()
        self._dedupe = dedupe or MemoryDedupeStore()
        self._dispatcher = EventDispatcher(
            clock=clock or SystemClock(),
            policy=resolved_policy,
            dead_letters=self._dead_letter_store,
            dedupe=self._dedupe,
            sleeper=sleeper,
        )
        self._lock = asyncio.Lock()

    def subscribe(self, topic: Topic, handler: EventHandler, *, subscriber: str) -> None:
        self._subscriptions.setdefault(topic, []).append(
            Subscription(topic=topic, subscriber=subscriber, handler=handler)
        )

    async def publish(self, envelope: EventEnvelope) -> None:
        """Deliver to every subscriber of the topic, in registration order."""
        async with self._lock:
            self._published.append(envelope)
            subscriptions = list(self._subscriptions.get(envelope.topic, []))

        for subscription in subscriptions:
            outcome = await self._dispatcher.deliver(subscription, envelope)
            self._outcomes.append(outcome)

    async def handle_push(
        self, envelope: EventEnvelope, *, subscriber: str | None = None
    ) -> tuple[DeliveryOutcome, ...]:
        """Deliver an envelope that arrived over HTTP rather than in-process.

        The same routing the Pub/Sub bus uses, so the internal push endpoint
        behaves identically whichever transport the process is configured with.
        """
        outcomes = await route(
            self._dispatcher, self._all_subscriptions(), envelope, subscriber=subscriber
        )
        self._outcomes.extend(outcomes)
        return outcomes

    def _all_subscriptions(self) -> list[Subscription]:
        return [sub for subs in self._subscriptions.values() for sub in subs]

    def subscribers_for(self, topic: Topic) -> tuple[str, ...]:
        return tuple(sub.subscriber for sub in self._subscriptions.get(topic, []))

    async def dead_letters(self) -> Sequence[EventEnvelope]:
        return [record.envelope for record in self._dead_letter_store.records]

    @property
    def dead_letter_store(self) -> InMemoryDeadLetterStore:
        return self._dead_letter_store

    # ---- inspection helpers used by tests and the activity stream ----

    @property
    def published(self) -> Sequence[EventEnvelope]:
        """Every envelope published, in order."""
        return list(self._published)

    @property
    def dead_letter_records(self) -> Sequence[DeadLetterRecord]:
        return self._dead_letter_store.records

    @property
    def outcomes(self) -> Sequence[DeliveryOutcome]:
        """The delivery result for every (envelope, subscriber) pair, in order."""
        return list(self._outcomes)

    @property
    def dispatcher(self) -> EventDispatcher:
        return self._dispatcher

    def breaker_state(self, subscriber: str) -> str | None:
        """The circuit state for one subscriber, for the console's fleet rail."""
        for subs in self._subscriptions.values():
            for sub in subs:
                if sub.subscriber == subscriber:
                    return str(sub.breaker.state)
        return None

    def published_topics(self) -> list[Topic]:
        return [e.topic for e in self._published]

    def clear(self) -> None:
        self._published.clear()
        self._outcomes.clear()
        self._dead_letter_store.clear()
        if isinstance(self._dedupe, MemoryDedupeStore):
            self._dedupe.clear()
        for subs in self._subscriptions.values():
            for sub in subs:
                sub.breaker.reset()
