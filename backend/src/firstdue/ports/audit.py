"""Audit sink.

Every policy decision, emergency exception, injection block, and write action is
an audit event carrying ``correlation_id`` and ``causation_id``, so one causal
chain can be reconstructed end to end.

Audit detail is redacted at construction. No source internals, no bucket names,
no record contents, no prompt text -- field names and hashes only.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.policy import PolicyDecision


class AuditEventKind(StrEnum):
    GRANT_MINTED = "grant_minted"
    GRANT_REVOKED = "grant_revoked"
    EMERGENCY_EXCEPTION = "emergency_exception"
    INJECTION_BLOCKED = "injection_blocked"
    #: The document screen could not run, so the document was withheld
    #: from the model. Distinct from a document that screened to nothing:
    #: this records that nobody read it, which an investigator
    #: reconstructing a pass must be able to tell from "nothing found".
    SCREEN_UNAVAILABLE = "screen_unavailable"
    MODEL_OUTPUT_REJECTED = "model_output_rejected"
    WRITE_EXECUTED = "write_executed"
    WRITE_REPLAYED = "write_replayed"
    WRITE_COMPENSATED = "write_compensated"
    NOTIFICATION_SENT = "notification_sent"
    APPROVAL_GRANTED = "approval_granted"
    CIRCUIT_OPENED = "circuit_opened"
    CIRCUIT_CLOSED = "circuit_closed"
    RMS_FLUSHED = "rms_flushed"
    DEAD_LETTERED = "dead_lettered"


class AuditEvent(BaseModel):
    """One redacted, correlated audit record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    audit_id: str = Field(min_length=1, max_length=120)
    kind: AuditEventKind
    occurred_at: datetime

    actor: str = Field(min_length=1, max_length=120, description="agent id or human id")
    actor_version: str | None = Field(default=None, max_length=40)
    target: str | None = Field(default=None, max_length=200)

    incident_id: str | None = Field(default=None, max_length=120)
    address_id: str | None = Field(default=None, max_length=120)
    rule_id: str | None = Field(default=None, max_length=120)

    correlation_id: str = Field(min_length=1, max_length=120)
    causation_id: str | None = Field(default=None, max_length=120)

    #: Redacted key/value detail. Field names and hashes, never contents.
    detail: dict[str, str] = Field(default_factory=dict)


@runtime_checkable
class AuditSink(Protocol):
    async def record_decision(self, decision: PolicyDecision) -> None: ...

    async def record_event(self, event: AuditEvent) -> None: ...

    async def list_decisions(
        self,
        *,
        incident_id: str | None = None,
        agent_id: str | None = None,
        limit: int = 100,
    ) -> Sequence[PolicyDecision]: ...

    async def list_events(
        self,
        *,
        incident_id: str | None = None,
        kind: AuditEventKind | None = None,
        limit: int = 100,
    ) -> Sequence[AuditEvent]: ...
