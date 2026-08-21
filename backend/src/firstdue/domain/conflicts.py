"""Conflicts between sources.

Disagreement is signal. When the permit says two stories and the lidar measures
three, the system surfaces the conflict rather than averaging or picking a
winner -- because unpermitted construction is itself a structural risk.

**Existence and severity of a conflict are always computed by the deterministic
engine.** ``narration`` is the only field a model may author, and it can neither
create a conflict nor change its severity. A model that could invent a conflict
could also invent its absence.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from firstdue.domain.keys import CanonicalKey
from firstdue.errors import ValidationError


class ConflictStatus(StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"


class ConflictResolution(BaseModel):
    """How an open conflict was closed. Only a human observation can close one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    resolved_at: datetime
    #: The survey or IC-resolution record that settled it.
    resolving_record_id: str = Field(min_length=1, max_length=120)
    #: The fact written as a result. Both original facts remain stored.
    resolving_fact_id: str = Field(min_length=1, max_length=120)
    resolved_by: str = Field(min_length=1, max_length=120, description="human identifier")
    note: str | None = Field(default=None, max_length=2000)


class Conflict(BaseModel):
    """A deterministic finding that two or more active facts disagree."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    conflict_id: str = Field(min_length=1, max_length=120)
    address_id: str = Field(min_length=1, max_length=120)
    canonical_key: CanonicalKey

    #: The deterministic rule that fired. Cited in the UI reasoning trace.
    rule_id: str = Field(min_length=1, max_length=120)
    #: 1 (informational) .. 5 (life safety). Computed by the engine, never a model.
    severity: int = Field(ge=1, le=5)

    #: Every fact that participates. Two or more, and all of them stay stored.
    fact_ids: tuple[str, ...] = Field(min_length=2)

    #: Deterministic template text. Always present, model or no model.
    summary: str = Field(min_length=1, max_length=500)
    #: Optional model-composed prose. Never affects existence or severity.
    narration: str | None = Field(default=None, max_length=4000)

    detected_at: datetime
    status: ConflictStatus = ConflictStatus.OPEN
    resolution: ConflictResolution | None = None

    @model_validator(mode="after")
    def _check_status(self) -> Self:
        if self.status is ConflictStatus.RESOLVED and self.resolution is None:
            raise ValidationError(
                "a resolved conflict must carry its resolution record",
                details={"conflict_id": self.conflict_id},
            )
        if self.status is ConflictStatus.OPEN and self.resolution is not None:
            raise ValidationError(
                "an open conflict must not carry a resolution record",
                details={"conflict_id": self.conflict_id},
            )
        if len(set(self.fact_ids)) != len(self.fact_ids):
            raise ValidationError(
                "conflict fact_ids must be distinct", details={"conflict_id": self.conflict_id}
            )
        return self

    def narrate(self, narration: str) -> Conflict:
        """Attach model-composed prose. Severity and existence are unchanged."""
        return self.model_copy(update={"narration": narration})

    def resolve(self, resolution: ConflictResolution) -> Conflict:
        if self.status is ConflictStatus.RESOLVED:
            raise ValidationError(
                "conflict is already resolved", details={"conflict_id": self.conflict_id}
            )
        return self.model_copy(update={"status": ConflictStatus.RESOLVED, "resolution": resolution})
