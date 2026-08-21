"""Agent runtime protocol.

``ADKRuntime`` (Google ADK on Vertex AI) and ``FakeRuntime`` (deterministic,
credential-free) both satisfy this. No agent runs without a grant, and no agent
runs forever: ``deadline`` is part of the call signature, not an option.

**The runtime is the only way an agent runs.** A handler is registered against
the descriptor it implements, and ``invoke`` is what calls it -- so the grant
check, the deadline, the terminal state, and the run record are not things each
agent has to remember to do. An agent called directly would skip all four, which
is exactly the shape of the gap this protocol closed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Protocol, TypeAlias, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.enums import AgentRunStatus
from firstdue.domain.events import EventIdValue
from firstdue.domain.identity import IncidentGrant, StandingGrant
from firstdue.domain.registry import AgentDescriptor

__all__ = [
    "AgentHandler",
    "AgentInput",
    "AgentOutcome",
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


class AgentOutcome(BaseModel):
    """What an agent's own work produced.

    The runtime wraps this into an :class:`AgentResult` with the run id, the
    terminal status, and the timings. An agent reports what it wrote; it does
    not get to report whether it was allowed to.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    written_fact_ids: tuple[str, ...] = ()
    emitted_event_ids: tuple[str, ...] = ()
    write_action_ids: tuple[str, ...] = ()
    policy_decision_ids: tuple[str, ...] = ()


#: The work one agent does. Registered against a descriptor; called by the
#: runtime, under a grant the runtime has already checked, inside a deadline
#: the runtime enforces.
AgentHandler: TypeAlias = Callable[[AgentInput, "Grant"], Awaitable[AgentOutcome]]


@runtime_checkable
class AgentRuntime(Protocol):
    def register(self, agent_id: str, handler: AgentHandler) -> None:
        """Bind an agent id to the work it does.

        Registering twice for one agent id is a configuration error: two
        implementations of one catalogued agent means the catalog no longer
        describes what runs.
        """
        ...

    async def invoke(
        self,
        descriptor: AgentDescriptor,
        payload: AgentInput,
        grant: Grant,
        deadline: datetime | None = None,
    ) -> AgentResult:
        """Run one agent to a terminal state within ``deadline``."""
        ...
