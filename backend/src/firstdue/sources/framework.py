"""One managed source, wrapping one page fetcher.

Everything a municipal source needs and nothing a particular one needs:

* **Caching.** A district poll asks eleven sources about 3,800 structures. Most
  of those answers were the same an hour ago, and a cache with an explicit TTL
  is the difference between a poll that finishes and one that gets rate-limited
  into a breaker trip.
* **Rate limiting.** A token bucket driven by the injected clock, so the limit
  is enforced deterministically and a test can prove it without waiting.
* **Snapshots.** Every fetch produces a :class:`SourceSnapshot` with a stable
  ``snapshot_id`` that lands on every fact extracted from it. That id is how a
  brief replays against exactly the data that produced it.
* **Circuit breaking.** Three consecutive failures and the source stops being
  asked; callers render ``UNAVAILABLE``, never ``NONE``.
* **Honest availability.** :meth:`health` says whether the records came from a
  live feed, a deterministic fixture, or nothing at all.

The fetcher below it is deliberately tiny: given an address, a watermark, and a
cursor, return a page. A live fetcher makes an HTTP call; a fixture fetcher
reads a file. Neither knows about caching, breakers, or snapshots.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, Protocol, runtime_checkable

from firstdue.domain.enums import Classification, SourceType
from firstdue.errors import SourceUnavailableError
from firstdue.observability.logging import get_logger
from firstdue.observability.tracing import source_query_span
from firstdue.ports.clock import Clock
from firstdue.ports.sources import SourceHealth, SourceMode, SourceRecord, SourceSnapshot
from firstdue.reliability.breaker import CircuitBreaker

logger = get_logger(__name__)

DEFAULT_CACHE_TTL: Final[timedelta] = timedelta(minutes=15)
DEFAULT_PAGE_SIZE: Final[int] = 50


@dataclass(frozen=True, slots=True)
class RawPage:
    """One page of records as a fetcher returned them."""

    records: tuple[SourceRecord, ...]
    next_cursor: str | None = None


@runtime_checkable
class PageFetcher(Protocol):
    """The only thing a source-specific implementation has to provide."""

    @property
    def mode(self) -> SourceMode:
        """Live feed, deterministic fixture, or unconfigured."""
        ...

    async def fetch_page(
        self,
        *,
        address_id: str | None,
        since: datetime | None,
        cursor: str | None,
        page_size: int,
    ) -> RawPage:
        """Return one page.

        Raises:
            SourceUnavailableError: when the source cannot be reached. The
                managed source turns that into breaker state and an
                ``UNAVAILABLE`` answer, never an empty result.
        """
        ...


class RateLimiter:
    """A token bucket with no clock of its own.

    Time arrives as an argument, so the limit is enforced identically over a
    simulated hour and a real one -- and a test can prove the limit exists
    rather than sleeping to observe it.
    """

    def __init__(self, *, rate_per_second: float, burst: int = 5) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        if burst < 1:
            raise ValueError("burst must be at least 1")
        self._rate = rate_per_second
        self._burst = float(burst)
        self._tokens = float(burst)
        self._last: datetime | None = None

    @property
    def tokens(self) -> float:
        return self._tokens

    def take(self, now: datetime) -> float:
        """Consume one token. Returns the seconds a caller should wait first.

        Zero means "go now". A non-zero result is the debt the caller owes, and
        it is reported rather than slept through: whether to wait, queue, or
        drop is the caller's decision, not the limiter's.
        """
        if self._last is not None:
            elapsed = max(0.0, (now - self._last).total_seconds())
            self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
        self._last = now

        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return 0.0
        deficit = 1.0 - self._tokens
        self._tokens = 0.0
        return deficit / self._rate


@dataclass(frozen=True, slots=True)
class SourceConfig:
    """What a source is, independent of how it is fetched."""

    source_id: str
    source_type: SourceType
    classification: Classification
    #: Requests per second this source tolerates. Conservative by default.
    rate_per_second: float = 5.0
    burst: int = 5
    cache_ttl: timedelta = DEFAULT_CACHE_TTL
    page_size: int = DEFAULT_PAGE_SIZE
    failure_threshold: int = 3
    cooldown: timedelta = timedelta(seconds=30)


@dataclass(slots=True)
class _CacheEntry:
    snapshot: SourceSnapshot
    expires_at: datetime


class ManagedSource:
    """A source adapter with caching, rate limiting, breaking, and provenance."""

    def __init__(self, config: SourceConfig, fetcher: PageFetcher, *, clock: Clock) -> None:
        self._config = config
        self._fetcher = fetcher
        self._clock = clock
        self._breaker = CircuitBreaker(
            f"source:{config.source_id}",
            failure_threshold=config.failure_threshold,
            cooldown=config.cooldown,
        )
        self._limiter = RateLimiter(rate_per_second=config.rate_per_second, burst=config.burst)
        self._cache: dict[str, _CacheEntry] = {}
        self._last_success_at: datetime | None = None
        self._last_failure_reason: str | None = None
        self._last_snapshot_id: str | None = None
        self._throttled = False
        self.cache_hits = 0
        self.upstream_calls = 0

    # ------------------------------------------------------------- protocol

    @property
    def source_id(self) -> str:
        return self._config.source_id

    @property
    def source_type(self) -> SourceType:
        return self._config.source_type

    @property
    def classification(self) -> Classification:
        return self._config.classification

    @property
    def mode(self) -> SourceMode:
        return self._fetcher.mode

    async def fetch(
        self,
        *,
        address_id: str | None = None,
        since: datetime | None = None,
        cursor: str | None = None,
    ) -> SourceSnapshot:
        """Pull one page, from cache when it is fresh enough.

        Raises:
            SourceUnavailableError: when the breaker is open, the source is
                unconfigured, or the fetch failed. Callers render
                ``UNAVAILABLE`` -- never an absence of hazard.
        """
        with source_query_span(
            source_id=self.source_id,
            mode=str(self.mode),
            scoped_to_address=address_id is not None,
        ) as active:
            snapshot = await self._fetch(address_id=address_id, since=since, cursor=cursor)
            active.set_many(
                {
                    "source.snapshot_id": snapshot.snapshot_id,
                    "source.record_count": len(snapshot.records),
                    "source.complete": snapshot.complete,
                }
            )
            return snapshot

    async def _fetch(
        self,
        *,
        address_id: str | None,
        since: datetime | None,
        cursor: str | None,
    ) -> SourceSnapshot:
        now = self._clock.now()

        if self.mode is SourceMode.UNCONFIGURED:
            raise SourceUnavailableError(
                "source has no configured endpoint or fixture",
                details={"source_id": self.source_id, "mode": str(self.mode)},
            )

        if not self._breaker.allow(now):
            raise SourceUnavailableError(
                "source circuit is open",
                details={
                    "source_id": self.source_id,
                    "circuit_state": str(self._breaker.state),
                },
            )

        key = self._cache_key(address_id, since, cursor)
        cached = self._cache.get(key)
        if cached is not None and now < cached.expires_at:
            self.cache_hits += 1
            return cached.snapshot

        self._throttled = self._limiter.take(now) > 0.0
        self.upstream_calls += 1
        try:
            page = await self._fetcher.fetch_page(
                address_id=address_id,
                since=since,
                cursor=cursor,
                page_size=self._config.page_size,
            )
        except SourceUnavailableError as exc:
            self._record_failure(now, str(exc.code))
            raise
        except Exception as exc:
            self._record_failure(now, type(exc).__name__)
            raise SourceUnavailableError(
                "source fetch failed",
                details={"source_id": self.source_id},
            ) from exc

        self._breaker.record_success(now)
        self._last_success_at = now
        self._last_failure_reason = None

        snapshot = SourceSnapshot(
            source_id=self.source_id,
            snapshot_id=self._snapshot_id(key, now),
            fetched_at=now,
            records=page.records,
            next_cursor=page.next_cursor,
            complete=page.next_cursor is None,
        )
        self._last_snapshot_id = snapshot.snapshot_id
        self._cache[key] = _CacheEntry(snapshot=snapshot, expires_at=now + self._config.cache_ttl)
        return snapshot

    async def health(self) -> SourceHealth:
        snapshot = self._breaker.snapshot()
        return SourceHealth(
            source_id=self.source_id,
            circuit_state=snapshot.state,
            consecutive_failures=snapshot.consecutive_failures,
            last_success_at=self._last_success_at,
            last_failure_reason=self._last_failure_reason,
            mode=self.mode,
            classification=self.classification,
            cache_hits=self.cache_hits,
            upstream_calls=self.upstream_calls,
            throttled=self._throttled,
            last_snapshot_id=self._last_snapshot_id,
        )

    # ------------------------------------------------------------ internals

    def _record_failure(self, now: datetime, reason: str) -> None:
        opened = self._breaker.record_failure(now, error_code=reason)
        self._last_failure_reason = reason
        if opened:
            logger.warning(
                "source_circuit_opened",
                extra={"source_id": self.source_id, "error_code": reason},
            )

    @staticmethod
    def _cache_key(address_id: str | None, since: datetime | None, cursor: str | None) -> str:
        return "|".join(
            (
                address_id or "*",
                since.isoformat() if since else "*",
                cursor or "0",
            )
        )

    def _snapshot_id(self, key: str, now: datetime) -> str:
        """Stable within a fetch, unique across fetches.

        The time is part of it deliberately: two pulls of the same page an hour
        apart are two snapshots, and a fact must name the one it came from.
        """
        digest = hashlib.sha256(f"{self.source_id}|{key}|{now.isoformat()}".encode()).hexdigest()[
            :16
        ]
        return f"{self.source_id}:{now.isoformat()}:{digest}"

    # -------------------------------------------------------- test controls

    def clear_cache(self) -> None:
        self._cache.clear()

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker


@dataclass(slots=True)
class FailingFetcher:
    """A fetcher that always fails. Used to exercise degraded paths."""

    reason: str = "induced failure"
    source_mode: SourceMode = SourceMode.LIVE
    calls: int = field(default=0)

    @property
    def mode(self) -> SourceMode:
        return self.source_mode

    async def fetch_page(
        self,
        *,
        address_id: str | None,
        since: datetime | None,
        cursor: str | None,
        page_size: int,
    ) -> RawPage:
        self.calls += 1
        raise SourceUnavailableError(self.reason, details={"induced": "true"})


@dataclass(slots=True)
class UnconfiguredFetcher:
    """A source that exists in the catalog with nothing behind it.

    This is the honest state for a feed we have named but cannot reach -- an
    API that needs a key we do not have, a county system with no public
    endpoint. It reports ``UNCONFIGURED`` and every fetch raises, so the fact
    that follows is ``UNAVAILABLE`` rather than a silent absence.
    """

    note: str = "no endpoint configured"

    @property
    def mode(self) -> SourceMode:
        return SourceMode.UNCONFIGURED

    async def fetch_page(
        self,
        *,
        address_id: str | None,
        since: datetime | None,
        cursor: str | None,
        page_size: int,
    ) -> RawPage:
        raise SourceUnavailableError(self.note, details={"mode": str(SourceMode.UNCONFIGURED)})


def paginate(records: Sequence[SourceRecord], cursor: str | None, page_size: int) -> RawPage:
    """Slice a materialised record list into a cursor-paginated page.

    Shared by every fetcher that holds its records in memory, so fixture-backed
    and live-cached sources paginate identically.
    """
    start = int(cursor) if cursor else 0
    page = tuple(records[start : start + page_size])
    next_start = start + len(page)
    return RawPage(
        records=page,
        next_cursor=str(next_start) if next_start < len(records) else None,
    )
