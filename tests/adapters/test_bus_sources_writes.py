"""Event delivery, circuit breakers, and idempotent external writes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from firstdue.adapters.clock import DeterministicIdGenerator, FixedClock
from firstdue.adapters.fake.sources import FakeSourceAdapter
from firstdue.adapters.fake.writes import FakeWriteTarget
from firstdue.adapters.memory.bus import InMemoryEventBus
from firstdue.domain.enums import (
    CircuitState,
    Classification,
    Department,
    Operation,
    SourceType,
)
from firstdue.domain.events import EventEnvelope, Topic
from firstdue.domain.work import WriteAction
from firstdue.errors import IdempotencyMismatchError, SourceUnavailableError
from firstdue.ports.sources import SourceRecord

NOW = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)


def _envelope(n: int, key: str | None = None) -> EventEnvelope:
    return EventEnvelope(
        event_id=f"ev-{n}",
        topic=Topic.FACT_WRITTEN,
        occurred_at=NOW,
        producer="records-watcher",
        producer_version="1.0.0",
        correlation_id="corr-1",
        ids={"address_id": "sf-0450-hayes"},
        idempotency_key=key or f"idem-key-{n:04d}",
    )


# --------------------------------------------------------------------- bus --


async def test_events_deliver_in_publish_order() -> None:
    bus = InMemoryEventBus()
    seen: list[str] = []

    async def handler(envelope: EventEnvelope) -> None:
        seen.append(envelope.event_id)

    bus.subscribe(Topic.FACT_WRITTEN, handler, subscriber="conflict-engine")
    for n in range(4):
        await bus.publish(_envelope(n))
    assert seen == ["ev-0", "ev-1", "ev-2", "ev-3"]


@pytest.mark.idempotency
async def test_redelivery_does_not_re_run_a_consumer() -> None:
    """At-least-once delivery, exactly-once effect."""
    bus = InMemoryEventBus()
    runs = 0

    async def handler(envelope: EventEnvelope) -> None:
        nonlocal runs
        runs += 1

    bus.subscribe(Topic.FACT_WRITTEN, handler, subscriber="conflict-engine")
    await bus.publish(_envelope(1, key="same-key-000001"))
    await bus.publish(_envelope(2, key="same-key-000001"))
    assert runs == 1


async def test_each_subscriber_receives_the_event() -> None:
    bus = InMemoryEventBus()
    seen: list[str] = []

    async def a(envelope: EventEnvelope) -> None:
        seen.append("a")

    async def b(envelope: EventEnvelope) -> None:
        seen.append("b")

    bus.subscribe(Topic.FACT_WRITTEN, a, subscriber="geometry-watcher")
    bus.subscribe(Topic.FACT_WRITTEN, b, subscriber="conflict-engine")
    await bus.publish(_envelope(0))
    assert seen == ["a", "b"]


@pytest.mark.degraded
async def test_a_poison_message_dead_letters_and_does_not_propagate() -> None:
    bus = InMemoryEventBus(max_attempts=3)
    attempts = 0

    async def failing(envelope: EventEnvelope) -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("consumer exploded")

    bus.subscribe(Topic.FACT_WRITTEN, failing, subscriber="broken")
    await bus.publish(_envelope(0))  # must not raise

    assert attempts == 3
    dead = await bus.dead_letters()
    assert len(dead) == 1
    assert bus.dead_letter_records[0].subscriber == "broken"


@pytest.mark.degraded
async def test_one_failing_subscriber_does_not_starve_another() -> None:
    bus = InMemoryEventBus(max_attempts=2)
    delivered: list[str] = []

    async def failing(envelope: EventEnvelope) -> None:
        raise RuntimeError("boom")

    async def healthy(envelope: EventEnvelope) -> None:
        delivered.append(envelope.event_id)

    bus.subscribe(Topic.FACT_WRITTEN, failing, subscriber="broken")
    bus.subscribe(Topic.FACT_WRITTEN, healthy, subscriber="ok")
    await bus.publish(_envelope(0))
    assert delivered == ["ev-0"]


# ----------------------------------------------------------------- sources --


def _source(clock: FixedClock, records: tuple[SourceRecord, ...] = ()) -> FakeSourceAdapter:
    return FakeSourceAdapter(
        source_id="sf-permits",
        source_type=SourceType.PERMIT,
        classification=Classification.PUBLIC,
        clock=clock,
        records=records,
        page_size=2,
    )


def _record(n: int) -> SourceRecord:
    return SourceRecord(
        record_ref=f"permit/{n}",
        address_id="sf-0450-hayes",
        classification=Classification.PUBLIC,
        fields={"permit_number": f"2026-{n:05d}"},
        observed_at=NOW,
    )


@pytest.mark.degraded
async def test_three_failures_open_the_circuit() -> None:
    clock = FixedClock(NOW)
    source = _source(clock)
    source.set_failing(True)

    for _ in range(3):
        with pytest.raises(SourceUnavailableError):
            await source.fetch()

    assert source.circuit_state is CircuitState.OPEN
    attempts_before = source.fetch_attempts

    # While open, the source is not touched at all.
    with pytest.raises(SourceUnavailableError):
        await source.fetch()
    assert source.fetch_attempts == attempts_before


@pytest.mark.degraded
async def test_a_half_open_probe_closes_the_circuit_after_recovery() -> None:
    clock = FixedClock(NOW)
    source = _source(clock, (_record(1),))
    source.set_failing(True)
    for _ in range(3):
        with pytest.raises(SourceUnavailableError):
            await source.fetch()

    source.set_failing(False)
    clock.advance(timedelta(seconds=31))
    snapshot = await source.fetch()

    assert source.circuit_state is CircuitState.CLOSED
    assert len(snapshot.records) == 1


@pytest.mark.degraded
async def test_a_failed_probe_reopens_the_circuit() -> None:
    clock = FixedClock(NOW)
    source = _source(clock)
    source.set_failing(True)
    for _ in range(3):
        with pytest.raises(SourceUnavailableError):
            await source.fetch()

    clock.advance(timedelta(seconds=31))
    with pytest.raises(SourceUnavailableError):
        await source.fetch()
    assert source.circuit_state is CircuitState.OPEN


async def test_pagination_is_resumable() -> None:
    clock = FixedClock(NOW)
    source = _source(clock, tuple(_record(n) for n in range(5)))

    first = await source.fetch()
    assert len(first.records) == 2
    assert first.complete is False and first.next_cursor == "2"

    second = await source.fetch(cursor=first.next_cursor)
    assert len(second.records) == 2

    third = await source.fetch(cursor=second.next_cursor)
    assert len(third.records) == 1 and third.complete is True


async def test_health_reports_the_breaker_state() -> None:
    clock = FixedClock(NOW)
    source = _source(clock, (_record(1),))
    await source.fetch()
    health = await source.health()
    assert health.circuit_state is CircuitState.CLOSED
    assert health.consecutive_failures == 0
    assert health.last_success_at == NOW


# ------------------------------------------------------------ write targets --


def _target(clock: FixedClock) -> FakeWriteTarget:
    return FakeWriteTarget(
        target_id="building-referral-intake",
        receiving_department=Department.BUILDING,
        clock=clock,
        ids=DeterministicIdGenerator("test"),
        external_ref_prefix="BLD",
    )


def _action(key: str = "referral-key-0001", payload_hash: str = "0123456789abcdef") -> WriteAction:
    return WriteAction(
        action_id="wa-1",
        agent_id="delta-ranker",
        agent_version="1.0.0",
        target="building-referral-intake",
        receiving_department=Department.BUILDING,
        operation=Operation.WRITE,
        idempotency_key=key,
        payload_hash=payload_hash,
        intent="File an unpermitted-construction referral",
        compensating_action="withdraw_referral",
        created_at=NOW,
    )


@pytest.mark.idempotency
async def test_a_replayed_key_does_not_file_twice() -> None:
    """Duplicate Pub/Sub delivery cannot file the same referral twice."""
    target = _target(FixedClock(NOW))
    first = await target.execute(_action(), body={"address_id": "sf-0450-hayes"})
    second = await target.execute(_action(), body={"address_id": "sf-0450-hayes"})

    assert target.written_count() == 1
    assert second.external_ref == first.external_ref
    assert second.replayed is True and first.replayed is False


@pytest.mark.idempotency
async def test_a_replayed_key_with_a_different_body_is_a_conflict() -> None:
    target = _target(FixedClock(NOW))
    await target.execute(_action(), body={"address_id": "sf-0450-hayes"})
    with pytest.raises(IdempotencyMismatchError) as excinfo:
        await target.execute(
            _action(payload_hash="ffffffffffffffff"), body={"address_id": "sf-1215-fell"}
        )
    assert excinfo.value.http_status == 409


@pytest.mark.idempotency
async def test_distinct_keys_write_separately() -> None:
    target = _target(FixedClock(NOW))
    await target.execute(_action("referral-key-0001"), body={})
    await target.execute(_action("referral-key-0002"), body={})
    assert target.written_count() == 2


async def test_a_write_can_be_compensated() -> None:
    target = _target(FixedClock(NOW))
    receipt = await target.execute(_action(), body={})
    void = await target.compensate(receipt, reason="referral withdrawn by captain")
    assert target.is_compensated(receipt.receipt_id) is True
    assert "VOID" in void.external_ref


@pytest.mark.degraded
async def test_an_unreachable_target_raises_rather_than_reporting_success() -> None:
    target = _target(FixedClock(NOW))
    target.unavailable = True
    with pytest.raises(SourceUnavailableError):
        await target.execute(_action(), body={})
    assert target.written_count() == 0
