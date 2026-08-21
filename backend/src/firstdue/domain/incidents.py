"""Incidents.

An incident is opened by a CAD dispatch event, reads exactly one profile
snapshot, and is closed by an explicit call that revokes the grant and seals the
log. Benchmarks are timestamps of things that happened -- clerical record
keeping, never a tactical instruction.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from firstdue.domain.enums import BenchmarkType
from firstdue.errors import ValidationError


class IncidentStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class Benchmark(BaseModel):
    """An operational benchmark, timestamped as it occurs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    benchmark_id: str = Field(min_length=1, max_length=120)
    incident_id: str = Field(min_length=1, max_length=120)
    type: BenchmarkType
    occurred_at: datetime
    recorded_by: str = Field(min_length=1, max_length=120)
    note: str | None = Field(default=None, max_length=500)


class Incident(BaseModel):
    """One dispatch, from CAD event to sealed log."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    incident_id: str = Field(min_length=1, max_length=120)
    address_id: str = Field(min_length=1, max_length=120)
    district_id: str = Field(min_length=1, max_length=120)
    cad_ref: str = Field(min_length=1, max_length=120)

    alarm_level: int = Field(ge=1, le=5)
    jurisdiction_id: str = Field(min_length=1, max_length=120)
    responding_agency_id: str = Field(min_length=1, max_length=120)

    grant_id: str = Field(min_length=1, max_length=120)
    #: The exact snapshot the brief was built from, recorded for replay.
    profile_snapshot_id: str = Field(min_length=1, max_length=120)
    #: True when no pre-incident profile existed. The brief says so on screen.
    cold_start: bool = False

    status: IncidentStatus = IncidentStatus.OPEN
    dispatched_at: datetime
    opened_at: datetime
    closed_at: datetime | None = None

    benchmarks: tuple[Benchmark, ...] = ()

    @model_validator(mode="after")
    def _check_close(self) -> Self:
        if self.status is IncidentStatus.CLOSED and self.closed_at is None:
            raise ValidationError(
                "a closed incident must record when it closed",
                details={"incident_id": self.incident_id},
            )
        if self.closed_at is not None and self.closed_at < self.opened_at:
            raise ValidationError(
                "incident closed_at precedes opened_at",
                details={"incident_id": self.incident_id},
            )
        return self

    def elapsed_seconds(self, now: datetime) -> float:
        """Seconds since dispatch -- drives the elapsed clock and the timer."""
        end = self.closed_at if self.closed_at is not None else now
        return max(0.0, (end - self.dispatched_at).total_seconds())

    def with_benchmark(self, benchmark: Benchmark) -> Incident:
        if benchmark.incident_id != self.incident_id:
            raise ValidationError("benchmark belongs to a different incident")
        return self.model_copy(update={"benchmarks": (*self.benchmarks, benchmark)})

    def close(self, *, at: datetime) -> Incident:
        if self.status is IncidentStatus.CLOSED:
            return self
        return self.model_copy(update={"status": IncidentStatus.CLOSED, "closed_at": at})
