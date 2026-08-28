"""The pull bridge: real Pub/Sub delivery on a machine Google cannot reach.

Production delivery is push, to an authenticated Cloud Run endpoint. A laptop
has no such endpoint, so `EVENT_BACKEND=pubsub` there publishes real events and
receives none -- the bus works perfectly and the fleet looks dead. These cover
the three things that has to get right: it delivers through the *same* handler
the push endpoint uses, it never touches the deployed subscriptions, and it does
nothing at all unless it was asked for.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from firstdue.adapters.clock import FixedClock
from firstdue.adapters.pubsub.bus import PubSubEventBus
from firstdue.adapters.pubsub.pull import PubSubPullBridge
from firstdue.domain.events import EventEnvelope, Topic
from firstdue.errors import ConfigurationError
from firstdue.eventing.pubsub_codec import encode

PROJECT = "firstdue-test"
NOW = datetime(2026, 8, 27, 8, 0, tzinfo=UTC)


class _Message:
    def __init__(self, data: bytes) -> None:
        self.data = data


class _Received:
    def __init__(self, ack_id: str, data: bytes) -> None:
        self.ack_id = ack_id
        self.message = _Message(data)


class _Response:
    def __init__(self, received: list[_Received]) -> None:
        self.received_messages = received


class FakeSubscriber:
    """Enough of the client to drive the bridge, and a record of what it did."""

    def __init__(self, batches: list[list[_Received]] | None = None) -> None:
        self.created: list[dict[str, Any]] = []
        self.acked: list[list[str]] = []
        self._batches = batches or []
        self.pulls = 0
        self.create_error: Exception | None = None

    def subscription_path(self, project: str, subscription: str) -> str:
        return f"projects/{project}/subscriptions/{subscription}"

    def topic_path(self, project: str, topic: str) -> str:
        return f"projects/{project}/topics/{topic}"

    def create_subscription(self, request: dict[str, Any]) -> Any:
        if self.create_error is not None:
            raise self.create_error
        self.created.append(request)
        return request

    def pull(self, request: dict[str, Any], timeout: float | None = None) -> Any:
        self.pulls += 1
        if self._batches:
            return _Response(self._batches.pop(0))
        return _Response([])

    def acknowledge(self, request: dict[str, Any]) -> Any:
        self.acked.append(list(request["ack_ids"]))
        return request


class AlreadyExists(Exception):  # noqa: N818
    """Named to match the Google exception the bridge recognises by type name.

    The bridge matches on `type(exc).__name__`, so this double has to carry the
    provider's spelling rather than an `...Error` suffix the linter would
    prefer -- renaming it would silently stop exercising the branch.
    """


def _bus() -> PubSubEventBus:
    return PubSubEventBus(
        project_id=PROJECT,
        publisher=_NullPublisher(),
        clock=FixedClock(NOW),
        topic_prefix="firstdue",
    )


class _NullPublisher:
    def topic_path(self, project: str, topic: str) -> str:
        return f"projects/{project}/topics/{topic}"

    def publish(self, topic: str, data: bytes, **attrs: Any) -> Any:
        raise AssertionError("the bridge must never publish")


def _envelope() -> EventEnvelope:
    return EventEnvelope(
        event_id="evt_pull_1",
        topic=Topic.CONFLICT_DETECTED,
        occurred_at=NOW,
        correlation_id="corr_pull",
        ids={"conflict_id": "conflict_1"},
        producer="structure-watch",
        producer_version="1.0.0",
        idempotency_key="idem_pull_1",
    )


def _bridge(bus: PubSubEventBus, subscriber: FakeSubscriber) -> PubSubPullBridge:
    return PubSubPullBridge(
        bus=bus,
        project_id=PROJECT,
        subscriber=subscriber,
        prefix="local-demo",
        topic_prefix="firstdue",
        pull_timeout_s=0.01,
    )


def test_its_subscriptions_are_its_own_and_prefixed() -> None:
    """Never the deployed push subscriptions.

    A subscription is push or pull; converting one would stop delivering to the
    Cloud Run service that owns it. Pub/Sub fans a topic out to every
    subscription, so an additional one observes the same events without taking
    any away.
    """
    bridge = _bridge(_bus(), FakeSubscriber())
    name = bridge.subscription_id(Topic.CONFLICT_DETECTED)
    assert name.startswith("local-demo-")
    assert "firstdue" in name


def test_an_unprefixed_bridge_refuses_to_construct() -> None:
    """An unprefixed name could collide with a deployed subscription."""
    with pytest.raises(ConfigurationError):
        PubSubPullBridge(bus=_bus(), project_id=PROJECT, subscriber=FakeSubscriber(), prefix="")


def test_it_refuses_without_a_project() -> None:
    with pytest.raises(ConfigurationError):
        PubSubPullBridge(
            bus=_bus(), project_id="", subscriber=FakeSubscriber(), prefix="local-demo"
        )


@pytest.mark.asyncio
async def test_a_pulled_envelope_reaches_the_local_handler() -> None:
    """The point of the whole module.

    The same `handle_push` the production endpoint calls, so dedupe, retry
    classification, breakers and dead-lettering are the ones already under test.
    """
    bus = _bus()
    seen: list[EventEnvelope] = []

    async def handler(envelope: EventEnvelope) -> None:
        seen.append(envelope)

    bus.subscribe(Topic.CONFLICT_DETECTED, handler, subscriber="structure-watch")

    payload = encode(_envelope()).data
    subscriber = FakeSubscriber([[_Received("ack-1", payload)]])
    bridge = _bridge(bus, subscriber)

    await bridge.start([Topic.CONFLICT_DETECTED])
    await asyncio.sleep(0.05)
    await bridge.stop()

    assert [e.event_id for e in seen] == ["evt_pull_1"]
    assert subscriber.acked and subscriber.acked[0] == ["ack-1"]
    assert bridge.delivered == 1


@pytest.mark.asyncio
async def test_an_undecodable_message_is_acked_rather_than_redelivered() -> None:
    """Redelivering bytes that will not parse is an infinite loop with a bill.

    It cannot reach a dead-letter store either, because that store is keyed by
    envelope and there is no envelope.
    """
    bus = _bus()
    subscriber = FakeSubscriber([[_Received("ack-bad", b"not-an-envelope")]])
    bridge = _bridge(bus, subscriber)

    await bridge.start([Topic.CONFLICT_DETECTED])
    await asyncio.sleep(0.05)
    await bridge.stop()

    assert subscriber.acked == [["ack-bad"]]
    assert bridge.undecodable == 1
    assert bridge.delivered == 0


@pytest.mark.asyncio
async def test_an_existing_subscription_is_reused_rather_than_failing() -> None:
    """Restarting the demo must not lose what arrived between runs."""
    subscriber = FakeSubscriber()
    subscriber.create_error = AlreadyExists("exists")
    bridge = _bridge(_bus(), subscriber)

    await bridge.start([Topic.CONFLICT_DETECTED])
    await bridge.stop()

    assert subscriber.created == []


@pytest.mark.asyncio
async def test_a_real_creation_failure_is_not_swallowed() -> None:
    """A bridge that could not subscribe looks exactly like a quiet bus."""
    subscriber = FakeSubscriber()
    subscriber.create_error = PermissionError("no pubsub.subscriptions.create")
    bridge = _bridge(_bus(), subscriber)

    with pytest.raises(PermissionError):
        await bridge.start([Topic.CONFLICT_DETECTED])


def test_the_bus_reports_only_the_topics_it_actually_handles() -> None:
    """What the bridge subscribes to.

    Subscribing to every topic in the catalog would pull envelopes nothing here
    handles, and `handle_push` would dead-letter each as NO_SUBSCRIBER --
    turning a healthy quiet topic into a stream of undeliverables.
    """
    bus = _bus()

    async def handler(envelope: EventEnvelope) -> None:
        return None

    bus.subscribe(Topic.CONFLICT_DETECTED, handler, subscriber="structure-watch")
    assert bus.subscribed_topics() == (Topic.CONFLICT_DETECTED,)


def test_the_bridge_is_off_by_default() -> None:
    """A background loop consuming a real topic is not an accident to have."""
    from firstdue.settings import Settings

    assert Settings().pubsub_pull_bridge is False
