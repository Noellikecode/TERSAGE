"""The Pub/Sub transport, against a broker.

The codec is proved by pure tests; what this file proves is the part only a
broker can: that :class:`PubSubEventBus` publishes a real message to a real
topic, with the ordering key and attributes intact, and that what comes back out
decodes to the envelope that went in.

Two brokers satisfy it, and the tests do not care which they got:
``PUBSUB_EMULATOR_HOST`` for a local emulator, or ``PUBSUB_TEST_PROJECT`` for
real Pub/Sub reached through Application Default Credentials. The real broker is
the stronger evidence -- message ordering is the property under test here, and
it is exactly the kind of thing an emulator approximates.

Skips loudly unless one of them is configured. A skipped transport has proved
nothing, and CI fails the job if this suite reports a skip.

Every test creates its own topic and subscription under a unique prefix and
deletes both afterwards, so a real project does not accumulate them.
"""

from __future__ import annotations

import os
import uuid
import warnings
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from firstdue.adapters.clock import FixedClock
from firstdue.domain.events import EventEnvelope, Topic
from firstdue.eventing.pubsub_codec import decode, subscription_name, topic_name

EMULATOR_ENV = "PUBSUB_EMULATOR_HOST"
PROJECT_ENV = "PUBSUB_TEST_PROJECT"

EMULATOR_HOST = os.environ.get(EMULATOR_ENV)
REAL_PROJECT = os.environ.get(PROJECT_ENV)

#: The emulator accepts any project id; this one names where the data came from.
EMULATOR_PROJECT = "firstdue-local"

# The emulator wins when both are set, for the same reason it does in
# conftest.py: publishing a test suite's throwaway messages into a real
# project by accident is not a mistake that should be quiet.
PROJECT = EMULATOR_PROJECT if EMULATOR_HOST else (REAL_PROJECT or EMULATOR_PROJECT)
NOW = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _restore_broker_env(monkeypatch: pytest.MonkeyPatch) -> None:
    if EMULATOR_HOST:
        monkeypatch.setenv(EMULATOR_ENV, EMULATOR_HOST)
    if REAL_PROJECT:
        monkeypatch.setenv(PROJECT_ENV, REAL_PROJECT)


def _require_broker() -> None:
    if not EMULATOR_HOST and not REAL_PROJECT:
        pytest.skip(
            f"neither {EMULATOR_ENV} nor {PROJECT_ENV} is set; run "
            "`make up && make test-emulator` for the emulator, or set "
            "PUBSUB_TEST_PROJECT for a real broker"
        )
    pytest.importorskip("google.cloud.pubsub_v1")


@pytest.fixture
def prefix() -> str:
    """A unique topic prefix per test, so runs cannot see each other's messages."""
    return f"t{uuid.uuid4().hex[:8]}"


@pytest.fixture
def pubsub(prefix: str) -> Iterator[tuple[Any, Any, str, str]]:
    _require_broker()
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
        # Best effort, in this order: a subscription outlives its topic and
        # would keep the project cluttered. A cleanup failure must not turn a
        # passing transport test into a failing one.
        for delete in (
            lambda: subscriber.delete_subscription(subscription=subscription_path),
            lambda: publisher.delete_topic(topic=topic_path),
        ):
            try:
                delete()
            except Exception as exc:  # pragma: no cover - cleanup is best effort
                warnings.warn(
                    f"could not clean up Pub/Sub resource: {type(exc).__name__}",
                    stacklevel=2,
                )
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
