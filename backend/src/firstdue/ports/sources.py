"""Source adapter protocol -- one per municipal or federal source.

Every source declares the classification of what it returns, so the gateway can
reason about it without inspecting contents, and every source reports circuit
state, so an outage renders as ``UNAVAILABLE`` rather than as an absence of
hazard.

Ingested document text is untrusted data and is never interpreted as
instruction. Adapters return it as data on :attr:`SourceRecord.document_text`;
the ingest path screens it before anything else touches it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.enums import CircuitState, Classification, SourceType


class SourceRecord(BaseModel):
    """One record as the source returned it, before any extraction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_ref: str = Field(min_length=1, max_length=400)
    address_id: str | None = Field(default=None, max_length=120)
    classification: Classification
    #: Structured fields the source published.
    fields: dict[str, Any] = Field(default_factory=dict)
    #: Free text (inspection narrative, permit description). Untrusted input.
    document_text: str | None = Field(default=None, max_length=200_000)
    observed_at: datetime


class SourceSnapshot(BaseModel):
    """An immutable, referenceable pull from one source.

    ``snapshot_id`` lands on every fact extracted from it, which is how a brief
    replays against exactly the data that produced it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str = Field(min_length=1, max_length=120)
    snapshot_id: str = Field(min_length=1, max_length=200)
    fetched_at: datetime
    records: tuple[SourceRecord, ...] = ()
    #: Set when the source paginated and more remains -- backfill resumes here.
    next_cursor: str | None = Field(default=None, max_length=400)
    complete: bool = True


class SourceMode(StrEnum):
    """Where a source's records actually came from.

    Rendered in the console verbatim. A hidden simulation is worse than an
    admitted one, so a source backed by a fixture says ``FIXTURE`` rather than
    quietly passing for the live feed.
    """

    LIVE = "LIVE"
    FIXTURE = "FIXTURE"
    #: Configured, but with no live endpoint and no fixture. Returns UNAVAILABLE.
    UNCONFIGURED = "UNCONFIGURED"


class SourceHealth(BaseModel):
    """Honest availability metadata for one source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    circuit_state: CircuitState
    consecutive_failures: int = Field(ge=0)
    last_success_at: datetime | None = None
    last_failure_reason: str | None = Field(default=None, max_length=200)

    #: Live feed, deterministic fixture, or nothing at all.
    mode: SourceMode = SourceMode.FIXTURE
    #: The classification of what this source can return.
    classification: Classification | None = None
    #: How many fetches were served from cache rather than from the source.
    cache_hits: int = Field(default=0, ge=0)
    #: How many fetches actually reached the source.
    upstream_calls: int = Field(default=0, ge=0)
    #: True when the last call waited on the rate limiter.
    throttled: bool = False
    #: The most recent snapshot id, so a fact's provenance can be traced back.
    last_snapshot_id: str | None = Field(default=None, max_length=200)

    @property
    def is_available(self) -> bool:
        return self.circuit_state is not CircuitState.OPEN and self.mode is not (
            SourceMode.UNCONFIGURED
        )


@runtime_checkable
class SourceAdapter(Protocol):
    """A read-only municipal or federal source."""

    @property
    def source_id(self) -> str: ...

    @property
    def source_type(self) -> SourceType: ...

    @property
    def classification(self) -> Classification:
        """The highest classification this source can return."""
        ...

    async def fetch(
        self,
        *,
        address_id: str | None = None,
        since: datetime | None = None,
        cursor: str | None = None,
    ) -> SourceSnapshot:
        """Pull records.

        Raises:
            SourceUnavailableError: when unreachable or the breaker is open.
                Callers render ``UNAVAILABLE``; they never render ``NONE``.
        """
        ...

    async def health(self) -> SourceHealth: ...


@runtime_checkable
class SourceRegistry(Protocol):
    """The set of sources configured for a municipality."""

    def get(self, source_id: str) -> SourceAdapter: ...

    def all(self) -> Sequence[SourceAdapter]: ...
