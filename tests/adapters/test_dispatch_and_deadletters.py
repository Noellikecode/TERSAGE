"""Event delivery: dedupe, classification, retries, breakers, dead letters.

Every test here runs against the in-memory bus, which is the same
:class:`EventDispatcher` the Pub/Sub push path runs. The policy is not
reimplemented per transport, so proving it once proves it for both.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from firstdue.adapters.clock import FixedClock
from firstdue.adapters.memory.bus import InMemoryEventBus
from firstdue.adapters.memory.repositories import InMemoryIdempotencyRepository
from firstdue.domain.enums import CircuitState
from firstdue.domain.events import CURRENT_SCHEMA_VERSION, EventEnvelope, Topic
from firstdue.errors import NotAuthorizedError, SourceUnavailableError, ValidationError
from firstdue.eventing.dispatch import (
    DeliveryStatus,
    EventDispatcher,
    RepositoryDedupeStore,
    Subscription,
    VirtualSleeper,
)
from firstdue.reliability.retry import FailureClass, RetryPolicy

NOW = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)


def _envelope(n: int = 0, *, key: str | None = None, **overrides: object) -> EventEnvelope:
    payload: dict[str, object] = {
        "event_id": f"ev-{n}",
        "topic": Topic.FACT_WRITTEN,
        "occurred_at": NOW,
        "producer": "records-watcher",
        "producer_version": "1.0.0",
        "correlation_id": "corr-1",
        "ids": {"address_id": "sf-0450-hayes"},
        "idempotency_key": key or f"idem-key-{n:06d}",
    }
    payload.update(overrides)
    return EventEnvelope(**payload)  # type: ignore[arg-type]


def _bus(*, max_attempts: int = 3) -> InMemoryEventBus:
    return InMemoryEventBus(
        max_attempts=max_attempts,
        clock=FixedClock(NOW),
        policy=RetryPolicy(max_attempts=max_attempts, base_delay_ms=100, jitter_ratio=0.0),
    )


# ------------------------------------------------------------------- dedupe


@pytest.mark.idempotency
async def test_a_redelivered_envelope_runs_a_consumer_once() -> None:
    bus = _bus()
    runs = 0

    async def handler(envelope: EventEnvelope) -> None:
        nonlocal runs
        runs += 1

    bus.subscribe(Topic.FACT_WRITTEN, handler, subscriber="conflict-detector")
    await bus.publish(_envelope(1, key="same-key-000001"))
    await bus.publish(_envelope(2, key="same-key-000001"))

    assert runs == 1
    assert [o.status for o in bus.outcomes] == [
        DeliveryStatus.DELIVERED,
        DeliveryStatus.DEDUPED,
    ]


@pytest.mark.idempotency
async def test_two_consumers_of_one_topic_each_act_on_it() -> None:
    """Deduping globally would starve the second consumer."""
    bus = _bus()
    seen: list[str] = []

    async def a(envelope: EventEnvelope) -> None:
        seen.append("conflict-detector")

    async def b(envelope: EventEnvelope) -> None:
        seen.append("survey-ranker")

    bus.subscribe(Topic.FACT_WRITTEN, a, subscriber="conflict-detector")
    bus.subscribe(Topic.FACT_WRITTEN, b, subscriber="survey-ranker")
    await bus.publish(_envelope(1))

    assert seen == ["conflict-detector", "survey-ranker"]


@pytest.mark.idempotency
async def test_dedupe_can_be_backed_by_the_durable_idempotency_store() -> None:
    """What makes the HTTP push path safe across a Cloud Run instance swap."""
    repository = InMemoryIdempotencyRepository()
    bus = InMemoryEventBus(
        clock=FixedClock(NOW), dedupe=RepositoryDedupeStore(repository, scope_prefix="event")
    )
    runs = 0

    async def handler(envelope: EventEnvelope) -> None:
        nonlocal runs
        runs += 1

    bus.subscribe(Topic.FACT_WRITTEN, handler, subscriber="conflict-detector")
    await bus.publish(_envelope(1, key="durable-key-0001"))
    await bus.publish(_envelope(1, key="durable-key-0001"))

    assert runs == 1


# ------------------------------------------------- classification and retries


@pytest.mark.degraded
async def test_a_transient_failure_is_retried_then_dead_lettered() -> None:
    bus = _bus(max_attempts=3)
    attempts = 0

    async def failing(envelope: EventEnvelope) -> None:
        nonlocal attempts
        attempts += 1
        raise SourceUnavailableError("sf-permits is down")

    bus.subscribe(Topic.FACT_WRITTEN, failing, subscriber="records-watcher")
    await bus.publish(_envelope(1))  # must not raise

    assert attempts == 3
    record = bus.dead_letter_records[0]
    assert record.attempts == 3
    assert record.failure_class is FailureClass.TRANSIENT
    assert record.reason == "SOURCE_UNAVAILABLE"
    assert record.is_poison is False


@pytest.mark.degraded
async def test_a_poison_message_is_dead_lettered_on_the_first_attempt() -> None:
    """Retrying a message that is wrong is how a queue stops moving."""
    bus = _bus(max_attempts=5)
    attempts = 0

    async def failing(envelope: EventEnvelope) -> None:
        nonlocal attempts
        attempts += 1
        raise ValidationError("this envelope names a fact that cannot exist")

    bus.subscribe(Topic.FACT_WRITTEN, failing, subscriber="conflict-detector")
    await bus.publish(_envelope(1))

    assert attempts == 1
    record = bus.dead_letter_records[0]
    assert record.failure_class is FailureClass.POISON
    assert record.is_poison


@pytest.mark.degraded
async def test_a_refusal_is_not_retried_either() -> None:
    bus = _bus(max_attempts=5)
    attempts = 0

    async def refusing(envelope: EventEnvelope) -> None:
        nonlocal attempts
        attempts += 1
        raise NotAuthorizedError("grant does not carry write:rms")

    bus.subscribe(Topic.FACT_WRITTEN, refusing, subscriber="incident-recorder")
    await bus.publish(_envelope(1))

    assert attempts == 1
    assert bus.dead_letter_records[0].failure_class is FailureClass.PERMANENT


@pytest.mark.degraded
async def test_an_envelope_from_a_future_schema_is_poison_and_never_reaches_a_handler() -> None:
    bus = _bus()
    called = False

    async def handler(envelope: EventEnvelope) -> None:
        nonlocal called
        called = True

    bus.subscribe(Topic.FACT_WRITTEN, handler, subscriber="conflict-detector")
    await bus.publish(_envelope(1, schema_version=CURRENT_SCHEMA_VERSION + 1))

    assert called is False
    assert bus.dead_letter_records[0].reason == "UNSUPPORTED_SCHEMA_VERSION"


@pytest.mark.degraded
async def test_a_failure_then_a_success_delivers_without_a_dead_letter() -> None:
    bus = _bus(max_attempts=3)
    attempts = 0

    async def flaky(envelope: EventEnvelope) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise SourceUnavailableError("first call failed")

    bus.subscribe(Topic.FACT_WRITTEN, flaky, subscriber="records-watcher")
    await bus.publish(_envelope(1))

    assert attempts == 2
    assert bus.dead_letter_records == []
    outcome = bus.outcomes[0]
    assert outcome.status is DeliveryStatus.DELIVERED
    assert outcome.attempts == 2
    # The backoff was computed and imposed, not skipped.
    assert outcome.backoffs_ms == (100,)


@pytest.mark.degraded
async def test_the_backoff_schedule_grows_between_attempts() -> None:
    sleeper = VirtualSleeper()
    dispatcher = EventDispatcher(
        clock=FixedClock(NOW),
        policy=RetryPolicy(max_attempts=4, base_delay_ms=100, multiplier=2.0, jitter_ratio=0.0),
        sleeper=sleeper,
    )

    async def failing(envelope: EventEnvelope) -> None:
        raise SourceUnavailableError("down")

    subscription = Subscription(
        topic=Topic.FACT_WRITTEN, subscriber="records-watcher", handler=failing
    )
    outcome = await dispatcher.deliver(subscription, _envelope(1))

    assert outcome.status is DeliveryStatus.DEAD_LETTERED
    assert outcome.backoffs_ms == (100, 200, 400)
    assert sleeper.slept == [0.1, 0.2, 0.4]


@pytest.mark.degraded
async def test_one_failing_subscriber_does_not_starve_another() -> None:
    bus = _bus(max_attempts=2)
    delivered: list[str] = []

    async def failing(envelope: EventEnvelope) -> None:
        raise SourceUnavailableError("down")

    async def healthy(envelope: EventEnvelope) -> None:
        delivered.append(envelope.event_id)

    bus.subscribe(Topic.FACT_WRITTEN, failing, subscriber="broken")
    bus.subscribe(Topic.FACT_WRITTEN, healthy, subscriber="ok")
    await bus.publish(_envelope(1))

    assert delivered == ["ev-1"]
    assert [r.subscriber for r in bus.dead_letter_records] == ["broken"]


# ---------------------------------------------------------- circuit breakers


@pytest.mark.degraded
async def test_a_sick_consumer_is_cut_off_rather_than_fed_forever() -> None:
    bus = _bus(max_attempts=1)

    async def failing(envelope: EventEnvelope) -> None:
        raise SourceUnavailableError("down")

    bus.subscribe(Topic.FACT_WRITTEN, failing, subscriber="records-watcher")
    for n in range(3):
        await bus.publish(_envelope(n))

    assert bus.breaker_state("records-watcher") == str(CircuitState.OPEN)

    await bus.publish(_envelope(9))
    assert bus.outcomes[-1].status is DeliveryStatus.CIRCUIT_OPEN
    assert bus.outcomes[-1].error_code == "CIRCUIT_OPEN"


@pytest.mark.degraded
async def test_poison_messages_do_not_open_a_consumers_breaker() -> None:
    """A bad message says nothing about whether the consumer is healthy."""
    bus = _bus(max_attempts=1)

    async def poison(envelope: EventEnvelope) -> None:
        raise ValidationError("malformed")

    bus.subscribe(Topic.FACT_WRITTEN, poison, subscriber="conflict-detector")
    for n in range(5):
        await bus.publish(_envelope(n))

    assert bus.breaker_state("conflict-detector") == str(CircuitState.CLOSED)
    assert len(bus.dead_letter_records) == 5


@pytest.mark.degraded
async def test_an_open_breaker_recovers_after_its_cooldown() -> None:
    clock = FixedClock(NOW)
    bus = InMemoryEventBus(
        max_attempts=1,
        clock=clock,
        policy=RetryPolicy(max_attempts=1, base_delay_ms=1, jitter_ratio=0.0),
    )
    fail = True

    async def flaky(envelope: EventEnvelope) -> None:
        if fail:
            raise SourceUnavailableError("down")

    bus.subscribe(Topic.FACT_WRITTEN, flaky, subscriber="records-watcher")
    for n in range(3):
        await bus.publish(_envelope(n))
    assert bus.breaker_state("records-watcher") == str(CircuitState.OPEN)

    fail = False
    clock.advance(timedelta(seconds=31))
    await bus.publish(_envelope(10))

    assert bus.outcomes[-1].status is DeliveryStatus.DELIVERED
    assert bus.breaker_state("records-watcher") == str(CircuitState.CLOSED)


# ------------------------------------------------------------- push routing


async def test_a_pushed_envelope_routes_to_the_named_subscriber_only() -> None:
    bus = _bus()
    seen: list[str] = []

    async def a(envelope: EventEnvelope) -> None:
        seen.append("conflict-detector")

    async def b(envelope: EventEnvelope) -> None:
        seen.append("survey-ranker")

    bus.subscribe(Topic.FACT_WRITTEN, a, subscriber="conflict-detector")
    bus.subscribe(Topic.FACT_WRITTEN, b, subscriber="survey-ranker")

    outcomes = await bus.handle_push(_envelope(1), subscriber="survey-ranker")
    assert seen == ["survey-ranker"]
    assert [o.subscriber for o in outcomes] == ["survey-ranker"]


async def test_dead_letters_are_listed_never_dropped() -> None:
    bus = _bus(max_attempts=1)

    async def failing(envelope: EventEnvelope) -> None:
        raise ValidationError("malformed")

    bus.subscribe(Topic.FACT_WRITTEN, failing, subscriber="conflict-detector")
    await bus.publish(_envelope(1))

    envelopes = await bus.dead_letters()
    assert [e.event_id for e in envelopes] == ["ev-1"]
    listed = await bus.dead_letter_store.list_all(subscriber="conflict-detector")
    assert len(listed) == 1
