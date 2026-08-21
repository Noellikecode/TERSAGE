"""Idempotency records -- the durable half of "exactly once".

An idempotency *key* only prevents duplicate work if something remembers having
seen it. That memory is this record, and it is deliberately shaped so the three
outcomes are distinguishable:

* **FRESH** -- nobody has claimed this key; the caller proceeds.
* **REPLAY** -- the same key with the same request; the caller returns the
  original result and performs no second effect.
* **MISMATCH** -- the same key with a *different* request. That is a caller bug
  or a collision, and it raises rather than guessing which body was meant.

A record is claimed *before* the work runs and completed after, so a crash
mid-effect leaves an ``IN_FLIGHT`` record that a later attempt can see and
reclaim once it has expired -- rather than a silent gap in which the work looks
untouched.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from firstdue.domain.work import IdempotencyKey
from firstdue.errors import ValidationError

#: How long an in-flight claim blocks a retry before it is considered abandoned.
DEFAULT_CLAIM_TTL = timedelta(minutes=10)


class IdempotencyStatus(StrEnum):
    IN_FLIGHT = "IN_FLIGHT"
    COMPLETED = "COMPLETED"


class IdempotencyOutcome(StrEnum):
    FRESH = "FRESH"
    REPLAY = "REPLAY"
    IN_PROGRESS = "IN_PROGRESS"


def request_hash(payload: Any) -> str:
    """Canonical hash of a request body, stable across processes and versions."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class IdempotencyRecord(BaseModel):
    """One claimed key within one scope."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: IdempotencyKey
    #: What the key is scoped to -- a subscriber name, or an external target id.
    #: Two consumers of the same event must each get to act on it once.
    scope: str = Field(min_length=1, max_length=200)
    request_hash: str = Field(min_length=8, max_length=128)

    status: IdempotencyStatus = IdempotencyStatus.IN_FLIGHT
    claimed_at: datetime
    #: When an abandoned in-flight claim may be taken over.
    claim_expires_at: datetime
    completed_at: datetime | None = None

    #: Identifier of whatever the original run produced -- receipt, fact, run id.
    result_ref: str | None = Field(default=None, max_length=200)
    correlation_id: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def _check_completion(self) -> Self:
        if self.status is IdempotencyStatus.COMPLETED and self.completed_at is None:
            raise ValidationError(
                "a completed idempotency record must record when it completed",
                details={"key": self.key, "scope": self.scope},
            )
        if self.claim_expires_at <= self.claimed_at:
            raise ValidationError(
                "an idempotency claim must expire after it was made",
                details={"key": self.key, "scope": self.scope},
            )
        return self

    @property
    def storage_id(self) -> str:
        """Document id: one record per (scope, key) pair."""
        digest = hashlib.sha256(f"{self.scope}|{self.key}".encode()).hexdigest()[:32]
        return f"idem_{digest}"

    def is_claimable(self, now: datetime) -> bool:
        """True when an in-flight claim has been abandoned long enough to retake."""
        return self.status is IdempotencyStatus.IN_FLIGHT and now >= self.claim_expires_at

    def completed(self, *, at: datetime, result_ref: str | None = None) -> IdempotencyRecord:
        return self.model_copy(
            update={
                "status": IdempotencyStatus.COMPLETED,
                "completed_at": at,
                "result_ref": result_ref if result_ref is not None else self.result_ref,
            }
        )


def storage_id_for(scope: str, key: str) -> str:
    """The document id a ``(scope, key)`` pair maps to."""
    digest = hashlib.sha256(f"{scope}|{key}".encode()).hexdigest()[:32]
    return f"idem_{digest}"


class IdempotencyClaim(BaseModel):
    """The result of trying to claim a key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: IdempotencyOutcome
    record: IdempotencyRecord

    @property
    def should_execute(self) -> bool:
        """Only a fresh claim runs the effect. Everything else is a no-op."""
        return self.outcome is IdempotencyOutcome.FRESH
