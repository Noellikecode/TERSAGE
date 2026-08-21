"""Fake source adapters with a real circuit breaker.

The breaker is the point. Three consecutive failures opens it; while open,
``fetch`` raises :class:`~firstdue.errors.SourceUnavailableError` without
touching the source, and callers render ``UNAVAILABLE`` -- never ``NONE``. After
the cooldown one half-open probe decides whether it closes again.

Fixtures loaded here are synthetic. No real person's records appear in them.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Final

from firstdue.domain.enums import CircuitState, Classification, SourceType
from firstdue.errors import SourceUnavailableError
from firstdue.ports.clock import Clock
from firstdue.ports.sources import SourceAdapter, SourceHealth, SourceRecord, SourceSnapshot

DEFAULT_FAILURE_THRESHOLD: Final[int] = 3
DEFAULT_COOLDOWN: Final[timedelta] = timedelta(seconds=30)
DEFAULT_PAGE_SIZE: Final[int] = 50


class FakeSourceAdapter:
    """An in-memory source that paginates, fails, and recovers like a real one."""

    def __init__(
        self,
        *,
        source_id: str,
        source_type: SourceType,
        classification: Classification,
        clock: Clock,
        records: Sequence[SourceRecord] = (),
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        cooldown: timedelta = DEFAULT_COOLDOWN,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        self._source_id = source_id
        self._source_type = source_type
        self._classification = classification
        self._clock = clock
        self._records = list(records)
        self._failure_threshold = failure_threshold
        self._cooldown = cooldown
        self._page_size = page_size

        self._failing = False
        self._consecutive_failures = 0
        self._state = CircuitState.CLOSED
        self._open_until: datetime | None = None
        self._last_success_at: datetime | None = None
        self._last_failure_reason: str | None = None
        self.fetch_attempts = 0

    # ------------------------------------------------------------- protocol

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def source_type(self) -> SourceType:
        return self._source_type

    @property
    def classification(self) -> Classification:
        return self._classification

    async def fetch(
        self,
        *,
        address_id: str | None = None,
        since: datetime | None = None,
        cursor: str | None = None,
    ) -> SourceSnapshot:
        now = self._clock.now()

        if self._state is CircuitState.OPEN:
            if self._open_until is not None and now < self._open_until:
                raise SourceUnavailableError(
                    "source circuit is open",
                    details={"source_id": self._source_id, "circuit_state": str(self._state)},
                )
            # Cooldown elapsed: allow exactly one probe.
            self._state = CircuitState.HALF_OPEN

        self.fetch_attempts += 1

        if self._failing:
            self._record_failure(now, "induced failure")
            raise SourceUnavailableError(
                "source unreachable",
                details={"source_id": self._source_id, "circuit_state": str(self._state)},
            )

        self._record_success(now)

        selected = [
            r
            for r in self._records
            if (address_id is None or r.address_id == address_id)
            and (since is None or r.observed_at >= since)
        ]
        start = int(cursor) if cursor else 0
        page = selected[start : start + self._page_size]
        next_start = start + len(page)
        has_more = next_start < len(selected)

        return SourceSnapshot(
            source_id=self._source_id,
            snapshot_id=f"{self._source_id}:{now.isoformat()}:{start}",
            fetched_at=now,
            records=tuple(page),
            next_cursor=str(next_start) if has_more else None,
            complete=not has_more,
        )

    async def health(self) -> SourceHealth:
        return SourceHealth(
            source_id=self._source_id,
            circuit_state=self._state,
            consecutive_failures=self._consecutive_failures,
            last_success_at=self._last_success_at,
            last_failure_reason=self._last_failure_reason,
        )

    # ----------------------------------------------------- breaker internals

    def _record_failure(self, now: datetime, reason: str) -> None:
        self._consecutive_failures += 1
        self._last_failure_reason = reason
        if (
            self._state is CircuitState.HALF_OPEN
            or self._consecutive_failures >= self._failure_threshold
        ):
            self._state = CircuitState.OPEN
            self._open_until = now + self._cooldown

    def _record_success(self, now: datetime) -> None:
        self._consecutive_failures = 0
        self._last_failure_reason = None
        self._last_success_at = now
        self._state = CircuitState.CLOSED
        self._open_until = None

    # -------------------------------------------------------- test controls

    def set_failing(self, failing: bool) -> None:
        """Induce or clear failures. Used by degraded-service tests."""
        self._failing = failing

    def add_records(self, records: Sequence[SourceRecord]) -> None:
        self._records.extend(records)

    @property
    def circuit_state(self) -> CircuitState:
        return self._state


class InMemorySourceRegistry:
    """The set of sources configured for a municipality."""

    def __init__(self, adapters: Sequence[SourceAdapter] = ()) -> None:
        self._adapters: dict[str, SourceAdapter] = {a.source_id: a for a in adapters}

    def register(self, adapter: SourceAdapter) -> None:
        self._adapters[adapter.source_id] = adapter

    def get(self, source_id: str) -> SourceAdapter:
        adapter = self._adapters.get(source_id)
        if adapter is None:
            raise SourceUnavailableError(
                "source is not configured for this municipality",
                details={"source_id": source_id},
            )
        return adapter

    def all(self) -> Sequence[SourceAdapter]:
        return [self._adapters[k] for k in sorted(self._adapters)]
