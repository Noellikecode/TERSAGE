"""The Pub/Sub transport, against the emulator.

The codec is proved by pure tests; what this file proves is the part only a
broker can: that :class:`PubSubEventBus` publishes a real message to a real
topic, with the ordering key and attributes intact, and that what comes back out
decodes to the envelope that went in.

Skips loudly unless ``PUBSUB_EMULATOR_HOST`` is set. A skipped transport has
proved nothing.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from firstdue.adapters.clock import FixedClock
from firstdue.domain.events import EventEnvelope, Topic
from firstdue.eventing.pubsub_codec import decode, subscription_name, topic_name

EMULATOR_ENV = "PUBSUB_EMULATOR_HOST"
EMULATOR_HOST = os.environ.get(EMULATOR_ENV)
PROJECT = "firstdue-local"
NOW = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _restore_emulator_host(monkeypatch: pytest.MonkeyPatch) -> None:
    if EMULATOR_HOST:
        monkeypatch.setenv(EMULATOR_ENV, EMULATOR_HOST)


def _require_emulator() -> None:
    if not EMULATOR_HOST:
        pytest.skip(f"{EMULATOR_ENV} is not set; run `make up` then `make test-emulator`")
    pytest.importorskip("google.cloud.pubsub_v1")


@pytest.fixture
def prefix() -> str:
    """A unique topic prefix per test, so runs cannot see each other's messages."""
    return f"t{uuid.uuid4().hex[:8]}"


@pytest.fixture
def pubsub(prefix: str) -> Iterator[tuple[Any, Any, str, str]]:
    _require_emulator()
    import google.cloud.pubsub_v1 as pubsub_v1

    publisher = pubsub_v1.PublisherClient(
        publisher_options=pubsub_v1.types.PublisherOptions(enable_message_ordering=True)
    )
    subscriber = pubsub_v1.SubscriberClient()

    topic_path = publisher.topic_path(PROJECT, topic_name(prefix, Topic.FACT_WRITTEN))
    subscription_path = subscriber.subscription_path(
        PROJECT, subscription_name(prefix, Topic.FACT_WRITTEN, "conflict-detector")
    )
    publisher.create_topic(name=topic_path)
    subscriber.create_subscription(
        request={
            "name": subscription_path,
            "topic": topic_path,
            "enable_message_ordering": True,
        }
    )
    try:
        yield publisher, subscriber, topic_path, subscription_path
    finally:
        subscriber.delete_subscription(subscription=subscription_path)
        publisher.delete_topic(topic=topic_path)
        subscriber.close()


def _envelope(n: int = 1) -> EventEnvelope:
    return EventEnvelope(
        event_id=f"ev-{n}",
        topic=Topic.FACT_WRITTEN,
        occurred_at=NOW,
        producer="records-watcher",
        producer_version="1.0.0",
        correlation_id="corr-1",
        ids={"address_id": "sf-0450-hayes", "fact_ids": ("fact-1", "fact-2")},
        idempotency_key=f"idem-key-{n:06d}",
    )


async def test_an_envelope_published_to_pubsub_comes_back_intact(
    pubsub: tuple[Any, Any, str, str], prefix: str
) -> None:
    from firstdue.adapters.pubsub.bus import PubSubEventBus

    publisher, subscriber, _topic_path, subscription_path = pubsub
    bus = PubSubEventBus(
        project_id=PROJECT,
        topic_prefix=prefix,
        clock=FixedClock(NOW),
        publisher=publisher,
    )
    envelope = _envelope()
    await bus.publish(envelope)

    pulled = subscriber.pull(subscription=subscription_path, max_messages=10, timeout=20)
    assert len(pulled.received_messages) == 1
    message = pulled.received_messages[0].message

    assert decode(message.data) == envelope
    assert message.ordering_key == "sf-0450-hayes"
    assert message.attributes["event_topic"] == str(Topic.FACT_WRITTEN)
    assert message.attributes["correlation_id"] == "corr-1"
    assert message.attributes["schema_version"] == "1"

    subscriber.acknowledge(
        subscription=subscription_path,
        ack_ids=[pulled.received_messages[0].ack_id],
    )


async def test_events_about_one_building_keep_their_order(
    pubsub: tuple[Any, Any, str, str], prefix: str
) -> None:
    """Pub/Sub orders within an ordering key, and the key is the building."""
    from firstdue.adapters.pubsub.bus import PubSubEventBus

    publisher, subscriber, _topic_path, subscription_path = pubsub
    bus = PubSubEventBus(
        project_id=PROJECT,
        topic_prefix=prefix,
        clock=FixedClock(NOW),
        publisher=publisher,
    )
    for n in range(3):
        await bus.publish(_envelope(n))

    received: list[EventEnvelope] = []
    while len(received) < 3:
        pulled = subscriber.pull(subscription=subscription_path, max_messages=10, timeout=20)
        if not pulled.received_messages:
            break
        for message in pulled.received_messages:
            received.append(decode(message.message.data))
        subscriber.acknowledge(
            subscription=subscription_path,
            ack_ids=[m.ack_id for m in pulled.received_messages],
        )

    assert [e.event_id for e in received] == ["ev-0", "ev-1", "ev-2"]
