"""Conflict persistence: the engines' output reaching durable storage.

Runs against both backends. What is under test is not the detection -- that is
covered purely in ``tests/unit/test_conflict_engine.py`` -- but everything
around it: the lock, the optimistic-concurrency write, the exactly-once
persistence, and the identifier-only announcement.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from firstdue.adapters.clock import DeterministicIdGenerator, FixedClock
from firstdue.adapters.memory.bus import InMemoryEventBus
from firstdue.container import Stores
from firstdue.domain.conflict_engine import PermitVersusLidarStoryCount
from firstdue.domain.enums import Classification, SourceType
from firstdue.domain.events import Topic
from firstdue.domain.facts import StructuralFact
from firstdue.domain.keys import Keys
from firstdue.domain.profiles import BuildingProfile, ProfileEvent, ProfileEventType
from firstdue.domain.values import IntegerValue
from firstdue.errors import NotFoundError, StaleVersionError
from firstdue.services.materialization import ProfileMaterializer

NOW = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)
ADDRESS = "sf-0450-hayes"
DISTRICT = "sffd-district-03"


def _fact(fact_id: str, *, stories: int, source_type: SourceType, days_ago: int) -> StructuralFact:
    return StructuralFact(
        fact_id=fact_id,
        address_id=ADDRESS,
        canonical_key=Keys.STORIES,
        value=IntegerValue(integer=stories),
        source_type=source_type,
        source_ref="ref",
        source_snapshot_id="snapshot-1",
        observed_at=NOW - timedelta(days=days_ago),
        ingested_at=NOW - timedelta(days=days_ago - 1),
        confidence=0.9,
        classification=Classification.PUBLIC,
    )


async def _seed_disagreement(stores: Stores) -> BuildingProfile:
    profile = BuildingProfile(address_id=ADDRESS, district_id=DISTRICT)
    for index, fact in enumerate(
        (
            _fact("fact-permit", stories=2, source_type=SourceType.PERMIT, days_ago=2870),
            _fact("fact-lidar", stories=3, source_type=SourceType.LIDAR_DSM, days_ago=410),
        )
    ):
        await stores.facts.append(fact)
        profile = profile.with_fact(
            fact,
            event=ProfileEvent(
                event_id=f"evt-{index}",
                sequence=profile.next_sequence,
                occurred_at=fact.ingested_at,
                type=ProfileEventType.FACT_WRITTEN,
                actor="records-watcher",
                summary="Recorded storey count.",
                fact_ids=(fact.fact_id,),
            ),
        )
    return await stores.profiles.create(profile)


def _materializer(stores: Stores, *, bus: InMemoryEventBus | None = None) -> ProfileMaterializer:
    return ProfileMaterializer(
        profiles=stores.profiles,
        conflicts=stores.conflicts,
        locks=stores.locks,
        clock=FixedClock(NOW),
        ids=DeterministicIdGenerator("contract"),
        bus=bus,
    )


async def test_a_detected_conflict_is_persisted_and_announced(stores: Stores) -> None:
    await _seed_disagreement(stores)
    bus = InMemoryEventBus(clock=FixedClock(NOW))
    outcome = await _materializer(stores, bus=bus).run(
        ADDRESS, owner="instance-a", correlation_id="corr-1"
    )

    assert outcome.ran and outcome.changed
    assert len(outcome.new_conflict_ids) == 1

    stored = await stores.conflicts.list_for_address(ADDRESS)
    assert [c.conflict_id for c in stored] == list(outcome.new_conflict_ids)
    assert stored[0].rule_id == PermitVersusLidarStoryCount.rule_id
    # Both facts are cited and both are still in the store.
    assert set(stored[0].fact_ids) == {"fact-permit", "fact-lidar"}
    assert len(await stores.facts.list_for_address(ADDRESS)) == 2

    published = bus.published
    assert [e.topic for e in published] == [Topic.CONFLICT_DETECTED]
    assert published[0].ids["conflict_id"] == stored[0].conflict_id
    assert published[0].correlation_id == "corr-1"


@pytest.mark.idempotency
async def test_running_twice_persists_one_conflict_and_bumps_nothing(stores: Stores) -> None:
    """The redelivery case, end to end through the durable stores."""
    await _seed_disagreement(stores)
    bus = InMemoryEventBus(clock=FixedClock(NOW))
    materializer = _materializer(stores, bus=bus)

    first = await materializer.run(ADDRESS, owner="instance-a", correlation_id="corr-1")
    second = await materializer.run(ADDRESS, owner="instance-a", correlation_id="corr-1")

    assert first.changed is True
    assert second.changed is False
    assert second.new_conflict_ids == ()
    assert second.profile_version == first.profile_version

    assert len(await stores.conflicts.list_for_address(ADDRESS)) == 1
    assert len(bus.published) == 1

    profile = await stores.profiles.get(ADDRESS)
    assert profile is not None
    assert len([e for e in profile.timeline if e.conflict_id]) == 1


@pytest.mark.concurrency
async def test_a_second_instance_does_not_repeat_the_work(stores: Stores) -> None:
    await _seed_disagreement(stores)
    materializer = _materializer(stores)

    results = await asyncio.gather(
        materializer.run(ADDRESS, owner="instance-a", correlation_id="corr-1"),
        materializer.run(ADDRESS, owner="instance-b", correlation_id="corr-2"),
    )

    # Exactly one did the work; the other found the lock held and stood down.
    assert sum(r.changed for r in results) == 1
    assert len(await stores.conflicts.list_for_address(ADDRESS)) == 1


@pytest.mark.concurrency
async def test_a_second_owner_finds_nothing_left_to_do(stores: Stores) -> None:
    """The lock frees on release, and the next holder sees a materialized profile."""
    await _seed_disagreement(stores)
    materializer = _materializer(stores)

    await materializer.run(ADDRESS, owner="instance-a", correlation_id="corr-1")
    outcome = await materializer.run(ADDRESS, owner="instance-b", correlation_id="corr-2")

    assert outcome.ran is True
    assert outcome.changed is False
    assert len(await stores.conflicts.list_for_address(ADDRESS)) == 1


@pytest.mark.concurrency
async def test_a_lost_version_race_is_reported_not_raised(stores: Stores) -> None:
    """The lock is an optimisation; the version check is the guarantee.

    A lease can expire while its holder is paused, so two workers can both reach
    the write. The loser is told and nothing is corrupted -- both computed the
    same result, because the engine is deterministic. This forces that branch by
    making the save lose the race.
    """
    await _seed_disagreement(stores)

    class LosesTheRace:
        """The profile repository, with one save that arrives too late."""

        def __init__(self, inner: object) -> None:
            self._inner = inner

        async def get(self, address_id: str) -> BuildingProfile | None:
            return await self._inner.get(address_id)  # type: ignore[attr-defined,no-any-return]

        async def save(self, profile: BuildingProfile, *, expected_version: int) -> object:
            raise StaleVersionError(
                expected=expected_version,
                actual=expected_version + 1,
                entity=f"profile {profile.address_id}",
            )

    materializer = ProfileMaterializer(
        profiles=LosesTheRace(stores.profiles),  # type: ignore[arg-type]
        conflicts=stores.conflicts,
        locks=stores.locks,
        clock=FixedClock(NOW),
        ids=DeterministicIdGenerator("contract"),
    )
    outcome = await materializer.run(ADDRESS, owner="instance-b", correlation_id="corr-2")

    assert outcome.ran is True
    assert outcome.contended is True
    assert outcome.changed is False
    # The conflict the losing pass detected was still persisted -- and exactly
    # once, because its id is derived from the facts rather than minted.
    assert len(await stores.conflicts.list_for_address(ADDRESS)) == 1


async def test_materializing_an_unknown_address_is_a_404(stores: Stores) -> None:
    with pytest.raises(NotFoundError):
        await _materializer(stores).run(
            "sf-9999-nowhere", owner="instance-a", correlation_id="corr-1"
        )


async def test_a_profile_with_no_disagreement_produces_nothing(stores: Stores) -> None:
    profile = BuildingProfile(address_id=ADDRESS, district_id=DISTRICT)
    fact = _fact("fact-permit", stories=2, source_type=SourceType.PERMIT, days_ago=100)
    await stores.facts.append(fact)
    profile = profile.with_fact(
        fact,
        event=ProfileEvent(
            event_id="evt-0",
            sequence=0,
            occurred_at=fact.ingested_at,
            type=ProfileEventType.FACT_WRITTEN,
            actor="records-watcher",
            summary="Recorded storey count.",
            fact_ids=(fact.fact_id,),
        ),
    )
    await stores.profiles.create(profile)

    bus = InMemoryEventBus(clock=FixedClock(NOW))
    outcome = await _materializer(stores, bus=bus).run(
        ADDRESS, owner="instance-a", correlation_id="corr-1"
    )

    assert outcome.new_conflict_ids == ()
    assert await stores.conflicts.list_for_address(ADDRESS) == []
    assert bus.published == []
    # Decay was still recomputed and stored, so the profile advanced once.
    assert outcome.changed is True
