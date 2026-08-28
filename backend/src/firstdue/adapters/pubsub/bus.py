"""The Pub/Sub event bus.

Publication goes out through the Pub/Sub client; delivery comes back in through
an authenticated HTTP push endpoint (see ``api/routes/internal.py``). That is
why ``subscribe`` here does not talk to Pub/Sub: subscriptions are infrastructure
(Terraform creates them), and what this class registers is the *local* handler
the push endpoint will route a delivered envelope to.

The delivery policy is not reimplemented here. ``handle_push`` runs the same
:class:`~firstdue.eventing.dispatch.EventDispatcher` the in-memory bus runs, so
dedupe, retry classification, breakers, and dead-lettering behave identically on
both paths.

The Google client is imported lazily, inside the publisher. Importing this
module requires no credentials and no ``google-cloud-pubsub`` install; only
actually publishing does.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, Final, Protocol, runtime_checkable

from firstdue.domain.events import EventEnvelope, Topic
from firstdue.errors import ConfigurationError
from firstdue.eventing.deadletter import DeadLetterRecord, InMemoryDeadLetterStore
from firstdue.eventing.dispatch import (
    DedupeStore,
    DeliveryOutcome,
    DeliveryStatus,
    EventDispatcher,
    Subscription,
    route,
)
from firstdue.eventing.pubsub_codec import encode, topic_name
from firstdue.observability.logging import get_logger
from firstdue.ports.bus import EventHandler
from firstdue.ports.clock import Clock
from firstdue.reliability.retry import DEFAULT_POLICY, FailureClass, RetryPolicy

logger = get_logger(__name__)

#: How long to wait for a publish to be accepted, seconds.
#:
#: Bounded because this is awaited inside an agent's budget. A broker that has
#: not accepted the message by now is an outage, and the caller's failure
#: classification is the right place for that -- not an unbounded wait.
PUBLISH_TIMEOUT_S: Final[float] = 10.0


@runtime_checkable
class PubSubPublisher(Protocol):
    """The slice of the Pub/Sub publisher client this adapter uses."""

    def topic_path(self, project: str, topic: str) -> str: ...

    def publish(self, topic: str, data: bytes, **attrs: Any) -> Any: ...


def build_publisher(*, enable_ordering: bool = True) -> PubSubPublisher:
    """Construct the real client. Imported here so nothing else needs it.

    Raises:
        ConfigurationError: when ``google-cloud-pubsub`` is not installed. Live
            mode fails loudly rather than falling back to an in-memory bus that
            would silently drop every cross-instance event.
    """
    try:
        import google.cloud.pubsub_v1 as pubsub_v1
    except ImportError as exc:  # pragma: no cover - exercised only in live mode
        raise ConfigurationError(
            "google-cloud-pubsub is not installed; install the 'google' extra "
            "or run with USE_FAKE_AGENTS=true",
            details={"package": "google-cloud-pubsub"},
        ) from exc

    settings = pubsub_v1.types.PublisherOptions(enable_message_ordering=enable_ordering)
    publisher: PubSubPublisher = pubsub_v1.PublisherClient(publisher_options=settings)
    return publisher


class PubSubEventBus:
    """Publishes to Pub/Sub; receives via the authenticated push endpoint."""

    def __init__(
        self,
        *,
        project_id: str,
        topic_prefix: str,
        clock: Clock,
        publisher: PubSubPublisher | None = None,
        policy: RetryPolicy = DEFAULT_POLICY,
        dedupe: DedupeStore | None = None,
        dead_letters: InMemoryDeadLetterStore | None = None,
    ) -> None:
        if not project_id:
            raise ConfigurationError("Pub/Sub requires a project id")
        self._project_id = project_id
        self._prefix = topic_prefix
        self._clock = clock
        self._publisher = publisher
        self._subscriptions: dict[Topic, list[Subscription]] = {}
        self._dead_letter_store = dead_letters or InMemoryDeadLetterStore()
        self._dispatcher = EventDispatcher(
            clock=clock,
            policy=policy,
            dead_letters=self._dead_letter_store,
            dedupe=dedupe,
        )
        self._published: list[EventEnvelope] = []

    # ------------------------------------------------------------- outbound

    def _client(self) -> PubSubPublisher:
        if self._publisher is None:
            self._publisher = build_publisher()
        return self._publisher

    def topic_for(self, topic: Topic) -> str:
        client = self._client()
        return client.topic_path(self._project_id, topic_name(self._prefix, topic))

    async def publish(self, envelope: EventEnvelope) -> None:
        """Publish one envelope. Ordering is keyed by the entity it concerns."""
        message = encode(envelope)
        client = self._client()
        future = client.publish(
            self.topic_for(envelope.topic),
            message.data,
            ordering_key=message.ordering_key,
            **message.attributes,
        )
        # Resolved, not fired and forgotten.
        #
        # `publish` returns a future and the client sends in a background
        # thread, so a failure -- a topic that does not exist, a permission
        # denied -- surfaces only when the future is read. It never was, so
        # every publish in live mode logged `event_published` and silently went
        # nowhere: the app's default `firstdue-` topic prefix did not match the
        # unprefixed topics Terraform creates, and nothing in the system said
        # so. A publish that failed must not read as one that succeeded.
        #
        # Off the event loop, because `result()` blocks until the send
        # completes and this runs under an agent's deadline.
        resolve = getattr(future, "result", None)
        if callable(resolve):
            await asyncio.to_thread(resolve, PUBLISH_TIMEOUT_S)
        self._published.append(envelope)
        logger.info(
            "event_published",
            extra={
                "topic": str(envelope.topic),
                "event_id": envelope.event_id,
                "schema_version": envelope.schema_version,
            },
        )

    # -------------------------------------------------------------- inbound

    def subscribe(self, topic: Topic, handler: EventHandler, *, subscriber: str) -> None:
        """Register the local handler a pushed envelope is routed to."""
        self._subscriptions.setdefault(topic, []).append(
            Subscription(topic=topic, subscriber=subscriber, handler=handler)
        )

    def subscribers_for(self, topic: Topic) -> tuple[str, ...]:
        return tuple(sub.subscriber for sub in self._subscriptions.get(topic, []))

    def subscribed_topics(self) -> tuple[Topic, ...]:
        """Topics this process has a local handler for.

        What the pull bridge subscribes to. Deliberately the *registered*
        topics rather than every topic in the catalog: a bridge that subscribed
        to all of them would pull envelopes nothing here handles, and
        ``handle_push`` would dead-letter each one as ``NO_SUBSCRIBER`` --
        turning a healthy quiet topic into a stream of undeliverables.
        """
        return tuple(sorted(self._subscriptions, key=str))

    async def handle_push(
        self, envelope: EventEnvelope, *, subscriber: str | None = None
    ) -> tuple[DeliveryOutcome, ...]:
        """Deliver a pushed envelope to its local subscribers.

        Args:
            envelope: the decoded envelope.
            subscriber: deliver only to this consumer. Pub/Sub pushes one
                subscription at a time, so the endpoint names which one; ``None``
                fans out to every local subscriber of the topic, which is what a
                replay harness wants.
        """
        subscriptions = [
            sub
            for sub in self._subscriptions.get(envelope.topic, [])
            if subscriber is None or sub.subscriber == subscriber
        ]
        if not subscriptions:
            # Nothing is registered for this topic in this process. Acking it
            # silently would lose it, so it is recorded as an undeliverable.
            record = DeadLetterRecord(
                envelope=envelope,
                subscriber=subscriber or "unrouted",
                attempts=1,
                reason="NO_SUBSCRIBER",
                failure_class=FailureClass.PERMANENT,
                dead_lettered_at=self._clock.now(),
            )
            await self._dead_letter_store.add(record)
            return (
                DeliveryOutcome(
                    status=DeliveryStatus.DEAD_LETTERED,
                    subscriber=subscriber or "unrouted",
                    event_id=envelope.event_id,
                    attempts=1,
                    error_code="NO_SUBSCRIBER",
                ),
            )
        return await route(self._dispatcher, subscriptions, envelope, subscriber=subscriber)

    async def dead_letters(self) -> Sequence[EventEnvelope]:
        return [record.envelope for record in self._dead_letter_store.records]

    @property
    def dead_letter_records(self) -> Sequence[DeadLetterRecord]:
        return self._dead_letter_store.records

    @property
    def published(self) -> Sequence[EventEnvelope]:
        return list(self._published)

    @property
    def dispatcher(self) -> EventDispatcher:
        return self._dispatcher

    @property
    def dead_letter_store(self) -> InMemoryDeadLetterStore:
        return self._dead_letter_store
