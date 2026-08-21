"""The cross-department agent registry.

Departments publish agents; other departments subscribe to a **pinned version**.
Pinning is not devops hygiene here -- a NIOSH line-of-duty-death investigation
has to reconstruct what an incident commander knew two years ago, so every brief
records the exact agent versions that produced it and must replay identically.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from firstdue.domain.enums import (
    ApprovalThreshold,
    Capability,
    Classification,
    Department,
    Loop,
    Scope,
)
from firstdue.domain.identity import WRITE_SCOPES
from firstdue.errors import ValidationError

SemVer = Annotated[
    str,
    StringConstraints(pattern=r"^\d+\.\d+\.\d+$", min_length=5, max_length=32),
]


class AgentDescriptor(BaseModel):
    """The catalog entry for one agent, at one version."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_id: str = Field(min_length=1, max_length=120)
    version: SemVer
    publisher_department: Department
    loop: Loop
    #: One line, shown in the catalog UI and the fleet rail.
    role_summary: str = Field(min_length=1, max_length=200)

    capabilities: frozenset[Capability] = Field(min_length=1)
    required_scopes: frozenset[Scope] = Field(min_length=1)
    classifications_accessed: frozenset[Classification] = Field(min_length=1)
    write_targets: tuple[str, ...] = ()
    approval_threshold: ApprovalThreshold = ApprovalThreshold.NONE

    input_schema_ref: str = Field(min_length=1, max_length=200)
    output_schema_ref: str = Field(min_length=1, max_length=200)
    latency_target_ms: int = Field(gt=0, le=600_000)

    published_at: datetime
    deprecated_at: datetime | None = None

    @model_validator(mode="after")
    def _check_capability_consistency(self) -> Self:
        writes = Capability.WRITE in self.capabilities
        if writes and not self.write_targets:
            raise ValidationError(
                "an agent with WRITE capability must declare its write targets",
                details={"agent_id": self.agent_id},
            )
        if not writes and self.write_targets:
            raise ValidationError(
                "an agent that declares write targets must declare WRITE capability",
                details={"agent_id": self.agent_id},
            )
        if writes and not (self.required_scopes & WRITE_SCOPES):
            raise ValidationError(
                "an agent with WRITE capability must require at least one write scope",
                details={"agent_id": self.agent_id},
            )
        return self

    @property
    def ref(self) -> str:
        """Stable ``agent_id@version`` reference recorded on every emission."""
        return f"{self.agent_id}@{self.version}"

    def is_deprecated(self, now: datetime) -> bool:
        return self.deprecated_at is not None and now >= self.deprecated_at


class Subscription(BaseModel):
    """A department binding itself to one pinned version of another's agent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subscription_id: str = Field(min_length=1, max_length=120)
    subscriber_department: Department
    agent_id: str = Field(min_length=1, max_length=120)
    #: Pinned. Never a range, never "latest".
    pinned_version: SemVer
    subscribed_at: datetime
    unsubscribed_at: datetime | None = None

    def is_active(self, now: datetime) -> bool:
        return self.unsubscribed_at is None or now < self.unsubscribed_at

    @property
    def ref(self) -> str:
        return f"{self.agent_id}@{self.pinned_version}"
