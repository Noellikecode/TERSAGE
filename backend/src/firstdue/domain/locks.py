"""Distributed processing locks.

Three thousand eight hundred structures are polled by a fleet that may be
running on several instances at once. Without a lock, two watchers extract the
same permit into two facts with two ids, and the conflict engine then reports
the building as disagreeing with itself.

Two properties make the lock safe rather than merely present:

* **It expires.** An instance that dies mid-poll must not hold a district
  forever, so every lease carries ``expires_at`` and a stale lease is
  reclaimable by anyone.
* **It fences.** Every acquisition increments a monotonic ``fence`` token. A
  process that pauses long enough to lose its lease and then wakes up and writes
  is detectable, because its fence is lower than the current holder's. Expiry
  alone cannot prevent that write; the fence is what makes it recognisable.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from firstdue.errors import ValidationError

#: Long enough for a district poll, short enough that a dead instance frees it.
DEFAULT_LEASE = timedelta(minutes=5)


class LockLease(BaseModel):
    """One held lock, owned by one process, until one moment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lock_id: str = Field(min_length=1, max_length=200, description="the resource, e.g. district id")
    owner: str = Field(min_length=1, max_length=120, description="instance or run id")
    acquired_at: datetime
    expires_at: datetime
    #: Monotonic per ``lock_id``. A write carrying a lower fence is stale.
    fence: int = Field(ge=1)

    @model_validator(mode="after")
    def _check_window(self) -> Self:
        if self.expires_at <= self.acquired_at:
            raise ValidationError(
                "a lock lease must expire after it was acquired",
                details={"lock_id": self.lock_id},
            )
        return self

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at

    def is_held_by(self, owner: str, *, now: datetime) -> bool:
        return self.owner == owner and not self.is_expired(now)

    def renewed(self, *, now: datetime, lease: timedelta = DEFAULT_LEASE) -> LockLease:
        """Extend the same lease. The fence does not change: it is the same holder."""
        return self.model_copy(update={"expires_at": now + lease})

    def remaining_seconds(self, now: datetime) -> float:
        return max(0.0, (self.expires_at - now).total_seconds())
