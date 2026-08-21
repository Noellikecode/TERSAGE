"""Caching, rate limiting, snapshots, breakers, and honest availability."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from firstdue.adapters.clock import FixedClock
from firstdue.domain.enums import CircuitState, Classification, SourceType
from firstdue.errors import SourceUnavailableError
from firstdue.ports.sources import SourceMode, SourceRecord
from firstdue.sources.framework import (
    FailingFetcher,
    ManagedSource,
    RateLimiter,
    RawPage,
    SourceConfig,
    UnconfiguredFetcher,
    paginate,
)

NOW = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)


def _record(n: int, *, address_id: str = "sf-0450-hayes") -> SourceRecord:
    return SourceRecord(
        record_ref=f"permit/{n:04d}",
        address_id=address_id,
        classification=Classification.PUBLIC,
        fields={"permit_number": f"{n:04d}"},
        observed_at=NOW - timedelta(days=n),
    )


class _CountingFetcher:
    """A fixture-shaped fetcher that counts how often it is actually called."""

    def __init__(self, records: list[SourceRecord]) -> None:
        self._records = records
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
        selected = [
            r
            for r in self._records
            if (address_id is None or r.address_id == address_id)
            and (since is None or r.observed_at >= since)
        ]
        return paginate(selected, cursor, page_size)


def _config(**overrides: object) -> SourceConfig:
    payload: dict[str, object] = {
        "source_id": "sf-permits",
        "source_type": SourceType.PERMIT,
        "classification": Classification.PUBLIC,
        "page_size": 2,
    }
    payload.update(overrides)
    return SourceConfig(**payload)  # type: ignore[arg-type]


# ------------------------------------------------------------------ caching


async def test_a_repeat_fetch_is_served_from_cache() -> None:
    fetcher = _CountingFetcher([_record(n) for n in range(3)])
    source = ManagedSource(_config(), fetcher, clock=FixedClock(NOW))

    first = await source.fetch()
    second = await source.fetch()

    assert fetcher.calls == 1
    assert second.snapshot_id == first.snapshot_id
    health = await source.health()
    assert health.cache_hits == 1
    assert health.upstream_calls == 1


async def test_the_cache_expires() -> None:
    clock = FixedClock(NOW)
    fetcher = _CountingFetcher([_record(0)])
    source = ManagedSource(_config(cache_ttl=timedelta(minutes=5)), fetcher, clock=clock)

    await source.fetch()
    clock.advance(timedelta(minutes=6))
    await source.fetch()

    assert fetcher.calls == 2


async def test_different_pages_are_cached_separately() -> None:
    fetcher = _CountingFetcher([_record(n) for n in range(5)])
    source = ManagedSource(_config(), fetcher, clock=FixedClock(NOW))

    first = await source.fetch()
    assert first.next_cursor is not None
    await source.fetch(cursor=first.next_cursor)
    await source.fetch()  # back to page one: cached

    assert fetcher.calls == 2


# --------------------------------------------------------------- pagination


async def test_pagination_walks_the_whole_source() -> None:
    fetcher = _CountingFetcher([_record(n) for n in range(5)])
    source = ManagedSource(_config(), fetcher, clock=FixedClock(NOW))

    seen: list[str] = []
    cursor: str | None = None
    while True:
        snapshot = await source.fetch(cursor=cursor)
        seen.extend(r.record_ref for r in snapshot.records)
        cursor = snapshot.next_cursor
        if cursor is None:
            break

    assert len(seen) == 5
    assert len(set(seen)) == 5


async def test_a_snapshot_id_is_recorded_for_provenance() -> None:
    source = ManagedSource(_config(), _CountingFetcher([_record(0)]), clock=FixedClock(NOW))
    snapshot = await source.fetch()

    assert snapshot.snapshot_id.startswith("sf-permits:")
    assert snapshot.complete is True
    health = await source.health()
    assert health.last_snapshot_id == snapshot.snapshot_id


# ------------------------------------------------------------ rate limiting


def test_the_rate_limiter_admits_a_burst_then_charges_for_waiting() -> None:
    limiter = RateLimiter(rate_per_second=2.0, burst=2)

    assert limiter.take(NOW) == 0.0
    assert limiter.take(NOW) == 0.0
    # Burst exhausted: the third call owes half a second at two per second.
    assert limiter.take(NOW) == pytest.approx(0.5)


def test_tokens_refill_over_time() -> None:
    limiter = RateLimiter(rate_per_second=2.0, burst=2)
    limiter.take(NOW)
    limiter.take(NOW)

    assert limiter.take(NOW + timedelta(seconds=1)) == 0.0


def test_the_limiter_never_exceeds_its_burst() -> None:
    limiter = RateLimiter(rate_per_second=10.0, burst=3)
    limiter.take(NOW)
    limiter.take(NOW + timedelta(hours=1))
    assert limiter.tokens <= 3.0


async def test_a_throttled_fetch_is_reported() -> None:
    fetcher = _CountingFetcher([_record(n) for n in range(20)])
    source = ManagedSource(
        _config(rate_per_second=1.0, burst=1, page_size=1), fetcher, clock=FixedClock(NOW)
    )
    await source.fetch()
    await source.fetch(cursor="1")
    assert (await source.health()).throttled is True


# --------------------------------------------------------------- degradation


@pytest.mark.degraded
async def test_repeated_failures_open_the_circuit() -> None:
    fetcher = FailingFetcher()
    source = ManagedSource(_config(failure_threshold=2), fetcher, clock=FixedClock(NOW))

    for _ in range(2):
        with pytest.raises(SourceUnavailableError):
            await source.fetch()

    health = await source.health()
    assert health.circuit_state is CircuitState.OPEN
    assert health.is_available is False

    # While open the source is not touched at all.
    with pytest.raises(SourceUnavailableError):
        await source.fetch()
    assert fetcher.calls == 2


@pytest.mark.degraded
async def test_an_unconfigured_source_says_so_and_refuses() -> None:
    """The honest state for a feed we have named but cannot reach."""
    source = ManagedSource(_config(), UnconfiguredFetcher(), clock=FixedClock(NOW))

    assert source.mode is SourceMode.UNCONFIGURED
    with pytest.raises(SourceUnavailableError):
        await source.fetch()

    health = await source.health()
    assert health.mode is SourceMode.UNCONFIGURED
    assert health.is_available is False


async def test_health_reports_the_classification_it_can_return() -> None:
    source = ManagedSource(
        _config(classification=Classification.TIER_II_CONFIDENTIAL),
        _CountingFetcher([]),
        clock=FixedClock(NOW),
    )
    health = await source.health()
    assert health.classification is Classification.TIER_II_CONFIDENTIAL
    assert health.mode is SourceMode.FIXTURE
