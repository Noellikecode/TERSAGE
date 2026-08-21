"""Interrupted backfill resumes from its checkpoint.

Polling eleven sources across a district is a job Cloud Run will interrupt. The
property under test is that an interrupted run does not restart the district and
does not skip the page it was working on.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from firstdue.adapters.clock import DeterministicIdGenerator, FixedClock
from firstdue.adapters.memory.repositories import InMemoryAgentRunRepository
from firstdue.domain.enums import AgentRunStatus, Classification, SourceType
from firstdue.ports.sources import SourceMode, SourceRecord
from firstdue.sources.backfill import DistrictBackfill
from firstdue.sources.framework import (
    FailingFetcher,
    ManagedSource,
    RawPage,
    SourceConfig,
    paginate,
)

NOW = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)
DISTRICT = "sffd-district-03"


class _Fetcher:
    def __init__(self, count: int, *, prefix: str) -> None:
        self._records = [
            SourceRecord(
                record_ref=f"{prefix}/{n:04d}",
                address_id=f"sf-{n:04d}-example",
                classification=Classification.PUBLIC,
                fields={},
                observed_at=NOW - timedelta(days=n),
            )
            for n in range(count)
        ]
        self.calls = 0

    @property
    def mode(self) -> SourceMode:
        return SourceMode.FIXTURE

    async def fetch_page(
        self,
        *,
        address_id: str | None,
        since: datetime | None,
        cursor: str | None,
        page_size: int,
    ) -> RawPage:
        self.calls += 1
        return paginate(self._records, cursor, page_size)


def _source(source_id: str, count: int, clock: FixedClock) -> ManagedSource:
    return ManagedSource(
        SourceConfig(
            source_id=source_id,
            source_type=SourceType.PERMIT,
            classification=Classification.PUBLIC,
            page_size=2,
            cache_ttl=timedelta(0),
        ),
        _Fetcher(count, prefix=source_id),
        clock=clock,
    )


@pytest.fixture
def backfill() -> tuple[DistrictBackfill, InMemoryAgentRunRepository, FixedClock]:
    clock = FixedClock(NOW)
    runs = InMemoryAgentRunRepository()
    return (
        DistrictBackfill(runs=runs, clock=clock, ids=DeterministicIdGenerator("backfill")),
        runs,
        clock,
    )


async def test_a_complete_backfill_pulls_every_source(backfill) -> None:
    engine, runs, clock = backfill
    sources = [_source("sf-permits", 5, clock), _source("sf-assessor", 3, clock)]

    result = await engine.run(district_id=DISTRICT, sources=sources, correlation_id="corr-1")

    assert result.status is AgentRunStatus.COMPLETED
    assert result.total_records == 8
    assert result.resumed is False
    assert [p.source_id for p in result.passes] == ["sf-permits", "sf-assessor"]

    run = await runs.get(result.run_id)
    assert run is not None
    assert run.is_terminal
    assert len(run.checkpoints) == 2


async def test_an_interrupted_backfill_stays_resumable(backfill) -> None:
    engine, runs, clock = backfill
    sources = [_source("sf-permits", 6, clock), _source("sf-assessor", 4, clock)]

    interrupted = await engine.run(
        district_id=DISTRICT,
        sources=sources,
        correlation_id="corr-1",
        interrupt_after_pages=1,
    )

    assert interrupted.status is AgentRunStatus.TIMED_OUT
    run = await runs.get(interrupted.run_id)
    assert run is not None
    assert run.is_resumable
    assert run.resume_point is not None
    # It stopped mid-source, on a real page boundary.
    assert run.resume_point.stage == "poll:sf-permits"
    assert run.resume_point.cursor == "2"


async def test_a_resumed_backfill_continues_from_the_checkpoint(backfill) -> None:
    engine, runs, clock = backfill
    sources = [_source("sf-permits", 6, clock), _source("sf-assessor", 4, clock)]

    interrupted = await engine.run(
        district_id=DISTRICT,
        sources=sources,
        correlation_id="corr-1",
        interrupt_after_pages=1,
    )
    before = sum(s._fetcher.calls for s in sources)

    resumed = await engine.run(
        district_id=DISTRICT,
        sources=sources,
        correlation_id="corr-1",
        resume_run_id=interrupted.run_id,
    )

    assert resumed.resumed is True
    assert resumed.status is AgentRunStatus.COMPLETED
    # It did not restart the district: the first page is not fetched again.
    after = sum(s._fetcher.calls for s in sources)
    assert after - before < 6

    # And the terminal record of the interrupted attempt survives.
    original = await runs.get(interrupted.run_id)
    assert original is not None
    assert original.status is AgentRunStatus.TIMED_OUT
    fresh = await runs.get(resumed.run_id)
    assert fresh is not None
    assert fresh.causation_id == interrupted.run_id
    assert fresh.attempt == 2


@pytest.mark.degraded
async def test_one_unavailable_source_does_not_stop_the_others(backfill) -> None:
    """A hazmat feed being down must never stop the permit feed."""
    engine, _runs, clock = backfill
    broken = ManagedSource(
        SourceConfig(
            source_id="tier-ii-confidential",
            source_type=SourceType.TIER_II,
            classification=Classification.TIER_II_CONFIDENTIAL,
        ),
        FailingFetcher(),
        clock=clock,
    )
    sources = [broken, _source("sf-permits", 4, clock)]

    result = await engine.run(district_id=DISTRICT, sources=sources, correlation_id="corr-1")

    assert result.status is AgentRunStatus.COMPLETED
    assert result.unavailable_sources == ("tier-ii-confidential",)
    # The healthy source still ran to completion.
    permits = next(p for p in result.passes if p.source_id == "sf-permits")
    assert permits.records == 4
    assert permits.available is True


async def test_the_backfill_key_is_derived_so_a_redispatch_finds_the_run(backfill) -> None:
    engine, _runs, _clock = backfill
    first = engine.idempotency_key(DISTRICT, None)
    second = engine.idempotency_key(DISTRICT, None)
    assert first == second
    assert engine.idempotency_key("sffd-district-05", None) != first
