"""Agent runtime protocol.

``ADKRuntime`` (Google ADK on Vertex AI) and ``FakeRuntime`` (deterministic,
credential-free) both satisfy this. No agent runs without a grant, and no agent
runs forever: ``deadline`` is part of the call signature, not an option.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, TypeAlias, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.enums import AgentRunStatus
from firstdue.domain.events import EventIdValue
from firstdue.domain.identity import IncidentGrant, StandingGrant
from firstdue.domain.registry import AgentDescriptor

__all__ = [
    "AgentInput",
    "AgentResult",
    "AgentRunStatus",
    "AgentRuntime",
    "Grant",
]

Grant: TypeAlias = IncidentGrant | StandingGrant


class AgentInput(BaseModel):
    """What an agent is asked to work on -- identifiers, never payloads."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1, max_length=120)
    causation_id: str | None = Field(default=None, max_length=120)
    ids: dict[str, EventIdValue] = Field(default_factory=dict)
    #: Small scalar parameters (district id, page cursor). Not record content.
    parameters: dict[str, str] = Field(default_factory=dict)


class AgentResult(BaseModel):
    """The terminal state of one agent run. Every run reaches one of these."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1, max_length=120)
    agent_ref: str = Field(min_length=1, max_length=160, description="agent_id@version")
    status: AgentRunStatus
    started_at: datetime
    finished_at: datetime

    written_fact_ids: tuple[str, ...] = ()
    emitted_event_ids: tuple[str, ...] = ()
    write_action_ids: tuple[str, ...] = ()
    policy_decision_ids: tuple[str, ...] = ()

    error_code: str | None = Field(default=None, max_length=80)
    #: Redacted. Never carries source internals or record contents.
    error_message: str | None = Field(default=None, max_length=400)

    @property
    def duration_ms(self) -> float:
        return (self.finished_at - self.started_at).total_seconds() * 1000.0


@runtime_checkable
class AgentRuntime(Protocol):
    async def invoke(
        self,
        descriptor: AgentDescriptor,
        payload: AgentInput,
        grant: Grant,
        deadline: datetime | None = None,
    ) -> AgentResult:
        """Run one agent to a terminal state within ``deadline``."""
        ...
