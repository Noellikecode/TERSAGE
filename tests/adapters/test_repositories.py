"""Repository behaviour: concurrency, append-only, and idempotent lookup."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from firstdue.adapters.memory.repositories import (
    InMemoryFactRepository,
    InMemoryIncidentLogRepository,
    InMemoryProfileRepository,
    InMemoryRegistryRepository,
    InMemoryWriteActionRepository,
)
from firstdue.domain.enums import (
    Capability,
    Classification,
    Department,
    LogEntryType,
    Loop,
    Scope,
)
from firstdue.domain.logentries import IncidentLogEntry
from firstdue.domain.profiles import BuildingProfile, ProfileEvent, ProfileEventType
from firstdue.domain.registry import AgentDescriptor, Subscription
from firstdue.errors import (
    AppendOnlyViolationError,
    NotFoundError,
    StaleVersionError,
)

NOW = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)


def _event(sequence: int) -> ProfileEvent:
    return ProfileEvent(
        event_id=f"evt-{sequence}",
        sequence=sequence,
        occurred_at=NOW,
        type=ProfileEventType.FACT_WRITTEN,
        actor="test",
        summary="recorded",
    )


@pytest.mark.concurrency
async def test_stale_write_is_rejected_with_409() -> None:
    repo = InMemoryProfileRepository()
    base = BuildingProfile(address_id="sf-0450-hayes", district_id="d1")
    await repo.create(base)

    # Two readers both read version 0.
    reader_a = await repo.get("sf-0450-hayes")
    reader_b = await repo.get("sf-0450-hayes")
    assert reader_a is not None and reader_b is not None

    await repo.save(reader_a.append_event(_event(0)), expected_version=0)

    with pytest.raises(StaleVersionError) as excinfo:
        await repo.save(reader_b.append_event(_event(0)), expected_version=0)
    assert excinfo.value.http_status == 409


@pytest.mark.concurrency
async def test_concurrent_writers_produce_exactly_one_winner() -> None:
    repo = InMemoryProfileRepository()
    await repo.create(BuildingProfile(address_id="a", district_id="d"))
    stored = await repo.get("a")
    assert stored is not None

    async def write() -> bool:
        try:
            await repo.save(stored.append_event(_event(0)), expected_version=0)
        except StaleVersionError:
            return False
        return True

    results = await asyncio.gather(*(write() for _ in range(5)))
    assert sum(results) == 1

    final = await repo.get("a")
    assert final is not None and final.profile_version == 1


@pytest.mark.concurrency
async def test_a_write_may_not_shorten_the_timeline() -> None:
    repo = InMemoryProfileRepository()
    await repo.create(BuildingProfile(address_id="a", district_id="d"))
    stored = await repo.get("a")
    assert stored is not None
    grown = stored.append_event(_event(0))
    await repo.save(grown, expected_version=0)

    truncated = grown.model_copy(update={"timeline": (), "profile_version": 2})
    with pytest.raises(AppendOnlyViolationError):
        await repo.save(truncated, expected_version=1)


async def test_saving_an_unknown_profile_is_not_found() -> None:
    repo = InMemoryProfileRepository()
    with pytest.raises(NotFoundError):
        await repo.save(BuildingProfile(address_id="missing", district_id="d"), expected_version=0)


async def test_facts_are_append_only(make_fact) -> None:
    repo = InMemoryFactRepository()
    fact = make_fact()
    await repo.append(fact)
    with pytest.raises(AppendOnlyViolationError):
        await repo.append(fact)
    assert len(await repo.list_for_address(fact.address_id)) == 1


async def test_incident_log_assigns_gapless_sequences() -> None:
    repo = InMemoryIncidentLogRepository()
    for sequence in range(3):
        await repo.append(
            IncidentLogEntry(
                entry_id=f"le-{sequence}",
                incident_id="inc-1",
                sequence=sequence,
                entry_type=LogEntryType.BRIEF_EMITTED,
                occurred_at=NOW,
                profile_snapshot_id="snap-1",
            )
        )
    log = await repo.get_log("inc-1")
    assert [e.sequence for e in log.entries] == [0, 1, 2]
    assert await repo.next_sequence("inc-1") == 3


async def test_sealed_incident_log_refuses_further_entries() -> None:
    repo = InMemoryIncidentLogRepository()
    await repo.append(
        IncidentLogEntry(
            entry_id="le-0",
            incident_id="inc-1",
            sequence=0,
            entry_type=LogEntryType.BENCHMARK,
            occurred_at=NOW,
            profile_snapshot_id="snap-1",
        )
    )
    await repo.seal("inc-1", at=NOW)
    with pytest.raises(AppendOnlyViolationError):
        await repo.append(
            IncidentLogEntry(
                entry_id="le-1",
                incident_id="inc-1",
                sequence=1,
                entry_type=LogEntryType.BENCHMARK,
                occurred_at=NOW,
                profile_snapshot_id="snap-1",
            )
        )


@pytest.mark.degraded
async def test_unflushed_entries_are_visible_until_the_rms_recovers() -> None:
    repo = InMemoryIncidentLogRepository()
    await repo.append(
        IncidentLogEntry(
            entry_id="le-0",
            incident_id="inc-1",
            sequence=0,
            entry_type=LogEntryType.BRIEF_EMITTED,
            occurred_at=NOW,
            profile_snapshot_id="snap-1",
        )
    )
    assert len(await repo.list_unflushed()) == 1
    await repo.mark_written_to_rms("inc-1", "le-0", at=NOW)
    assert len(await repo.list_unflushed()) == 0


def _descriptor(version: str = "1.0.0") -> AgentDescriptor:
    return AgentDescriptor(
        agent_id="hazard-watcher",
        version=version,
        publisher_department=Department.COUNTY_OEM,
        loop=Loop.SLOW,
        role_summary="Watches hazmat filings",
        capabilities=frozenset({Capability.READ}),
        required_scopes=frozenset({Scope.READ_TIER_II_METADATA}),
        classifications_accessed=frozenset({Classification.TIER_II_CONFIDENTIAL}),
        input_schema_ref="in.json",
        output_schema_ref="out.json",
        latency_target_ms=1000,
        published_at=NOW,
    )


async def test_a_published_version_is_immutable() -> None:
    repo = InMemoryRegistryRepository()
    await repo.publish(_descriptor())
    await repo.publish(_descriptor())  # identical republish is a no-op
    mutated = _descriptor().model_copy(update={"role_summary": "changed"})
    # A version somebody pinned must not turn into different code underneath
    # them, so this is an append-only violation (409), not a bad request.
    with pytest.raises(AppendOnlyViolationError) as excinfo:
        await repo.publish(mutated)
    assert excinfo.value.http_status == 409


async def test_subscription_resolves_the_pinned_version() -> None:
    repo = InMemoryRegistryRepository()
    await repo.publish(_descriptor("1.0.0"))
    await repo.publish(_descriptor("2.0.0"))
    await repo.subscribe(
        Subscription(
            subscription_id="sub-1",
            subscriber_department=Department.FIRE,
            agent_id="hazard-watcher",
            pinned_version="1.0.0",
            subscribed_at=NOW,
        )
    )
    resolved = await repo.resolve_pinned(Department.FIRE, "hazard-watcher")
    assert resolved is not None and resolved.version == "1.0.0"


async def test_cannot_subscribe_to_an_unpublished_version() -> None:
    repo = InMemoryRegistryRepository()
    await repo.publish(_descriptor("1.0.0"))
    with pytest.raises(NotFoundError):
        await repo.subscribe(
            Subscription(
                subscription_id="sub-2",
                subscriber_department=Department.FIRE,
                agent_id="hazard-watcher",
                pinned_version="9.9.9",
                subscribed_at=NOW,
            )
        )


@pytest.mark.idempotency
async def test_write_actions_are_findable_by_idempotency_key() -> None:
    from firstdue.domain.enums import Operation
    from firstdue.domain.work import WriteAction

    repo = InMemoryWriteActionRepository()
    action = WriteAction(
        action_id="wa-1",
        agent_id="delta-ranker",
        agent_version="1.0.0",
        target="building-referral-intake",
        receiving_department=Department.BUILDING,
        operation=Operation.WRITE,
        idempotency_key="referral-key-0001",
        payload_hash="0123456789abcdef",
        intent="file referral",
        compensating_action="withdraw_referral",
        created_at=NOW,
    )
    await repo.record(action)
    found = await repo.find_by_idempotency_key("building-referral-intake", "referral-key-0001")
    assert found is not None and found.action_id == "wa-1"
