"""The Incident Interceptor: one agent for the whole incident loop.

It supersedes ``incident-controller`` and ``brief-reconciler``. Those two were
split because opening an incident and assembling a brief sounded like different
work, and it turned out not to be: the controller emitted stage one of a
document and the reconciler emitted stages two and three of the same document,
which put the 500 ms budget and the model boundary on opposite sides of a
service boundary neither of them asked for.

The merge is also what gives the intake somewhere to live. Four things happen on
a dispatch, in this order, and the order is the whole safety argument:

1. **Open.** Mint the incident grant, read one profile snapshot, record it.
2. **Stage one.** Render the instant brief from that snapshot and persist it.
   No model is on this path -- not a fast one, not an optional one; the emission
   type refuses ``model_invoked=True`` on the instant stage. Budget 500 ms.
3. **Read the intake.** *After* stage one is on the record. A 911 transcript or
   a CAD narrative goes to Gemini for extraction, bounded and rejectable, and
   whatever it returns arrives as a **marked amendment** -- stage three, the
   stage that exists for late data. If Vertex is down, steps 1 and 2 already
   landed and nothing about them changes.
4. **Route.** A deterministic rule table, matched against the other incident
   agents' declared capabilities, decides who is woken and what each is handed.
   See :mod:`firstdue.incident.handoff` for why no part of that is a model call.

Two boundaries are worth stating in one place, because they are the ones that
would be quietly convenient to cross:

**The intake is never on the instant path.** Step 3 cannot delay step 2, because
step 2 has already been persisted and transmitted when step 3 starts. That is
not a scheduling preference; it is the reason the instant brief has a 500 ms
budget it can actually meet.

**The model never chooses who runs.** It reads a transcript into six typed
fields. Everything after that is a rule table and a capability match.

Nothing here recommends anything. The interceptor moves information and states
where each piece came from.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Final, Protocol

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.briefs import BriefEmission, BriefSection
from firstdue.domain.enums import Scope
from firstdue.domain.profiles import ProfileSnapshot
from firstdue.domain.registry import AgentDescriptor
from firstdue.incident.handoff import Handoff, RoutingPlan, plan_handoffs
from firstdue.incident.intake import (
    IntakeChannel,
    IntakeReader,
    IntakeReading,
    IntakeSignals,
    rejected_reading,
    reported_sections,
    signals_from,
)
from firstdue.observability.logging import get_logger
from firstdue.ports.repositories import RegistryRepository
from firstdue.registry.descriptors import active_descriptors

logger = get_logger(__name__)

#: The merged agent. Both superseded ids resolve to this one in the catalog, and
#: this is the id every emission, log entry and run record now names.
AGENT_ID: Final[str] = "incident-interceptor"


class AgentWaker(Protocol):
    """Whatever actually starts the routed agents.

    A protocol rather than a dependency on
    :class:`~firstdue.agents.fleet.FleetRunner`, because deciding who runs and
    running them are separable and only the first one is this module's argument.
    The session supplies the implementation that goes through the fleet, under
    the incident's own grant, with a durable run record -- and a unit test
    supplies one that records the handoff and does nothing, which is how the
    routing decision gets tested without a runtime.
    """

    async def wake(self, handoff: Handoff, *, incident_id: str, correlation_id: str) -> str | None:
        """Start one agent on one handoff. Returns a run id, or ``None``."""
        ...


class InterceptResult(BaseModel):
    """What reading and routing one intake produced."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    incident_id: str = Field(min_length=1, max_length=120)
    reading: IntakeReading
    signals: IntakeSignals
    plan: RoutingPlan
    #: The amendment carrying the reported items, when there were any to carry.
    #: Absent when the intake reported nothing -- an amendment that said nothing
    #: would still bump the brief version and make a commander re-read it.
    emission: BriefEmission | None = None
    #: Agents actually started, in plan order. Shorter than the plan when a wake
    #: failed; the plan is what was decided, this is what happened.
    woken_agent_ids: tuple[str, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.reading.accepted


class IncidentInterceptor:
    """Reads the intake, amends the brief, and routes the incident."""

    def __init__(
        self,
        *,
        intake: IntakeReader,
        registry: RegistryRepository | None = None,
        waker: AgentWaker | None = None,
        agent_id: str = AGENT_ID,
    ) -> None:
        self._intake = intake
        self._registry = registry
        self._waker = waker
        self._agent_id = agent_id

    # ------------------------------------------------------------- the read

    async def read_intake(
        self,
        narrative: str,
        *,
        incident_id: str,
        channel: IntakeChannel,
        source_ref: str,
    ) -> IntakeReading:
        """Read one narrative. Never raises; a failure is a rejected reading."""
        return await self._intake.read(
            narrative, incident_id=incident_id, channel=channel, source_ref=source_ref
        )

    # ---------------------------------------------------------- the routing

    async def route(
        self,
        reading: IntakeReading,
        *,
        now: datetime,
        authorised_scopes: frozenset[Scope] | None = None,
        descriptors: Sequence[AgentDescriptor] | None = None,
    ) -> RoutingPlan:
        """Plan the handoffs against the catalog this department subscribes to.

        The catalog comes from the registry when there is one, because the
        pinned version is what a department actually runs, and falls back to the
        shipped descriptors otherwise -- the same fallback
        :class:`~firstdue.agents.fleet.FleetRunner` makes, and for the same
        reason: a process that has not seeded a registry is a normal process,
        and routing nothing at all would be a very confusing outage.

        ``authorised_scopes`` is the incident grant's own scope set, and passing
        it is what keeps the plan inside this incident's authority rather than
        producing a denial per incident. It can only ever narrow the plan.
        """
        catalog = descriptors if descriptors is not None else await self._catalog()
        return plan_handoffs(
            reading,
            descriptors=catalog,
            now=now,
            self_agent_id=self._agent_id,
            authorised_scopes=authorised_scopes,
        )

    async def _catalog(self) -> Sequence[AgentDescriptor]:
        if self._registry is None:
            return active_descriptors()
        published = await self._registry.list_agents()
        return published if published else active_descriptors()

    async def wake_all(
        self, plan: RoutingPlan, *, incident_id: str, correlation_id: str
    ) -> tuple[str, ...]:
        """Start every agent the plan named, in plan order.

        One agent failing to start does not stop the next one. The incident's
        other agents are not each other's prerequisites, and an intake that
        woke two of three is strictly better than one that woke none because
        the first raised.
        """
        if self._waker is None:
            return ()
        started: list[str] = []
        for handoff in plan.handoffs:
            try:
                await self._waker.wake(
                    handoff, incident_id=incident_id, correlation_id=correlation_id
                )
            except Exception as exc:
                logger.warning(
                    "handoff_wake_failed",
                    extra={
                        "incident_id": incident_id,
                        "agent_id": handoff.agent_id,
                        "error_type": type(exc).__name__,
                    },
                )
                continue
            started.append(handoff.agent_id)
        return tuple(started)

    # ----------------------------------------------------------- the amendment

    @staticmethod
    def amendment_sections(
        reading: IntakeReading,
        signals: IntakeSignals,
        *,
        snapshot: ProfileSnapshot,
        cad_alarm_level: int,
    ) -> tuple[BriefSection, ...]:
        """The reported lines, ready to hang off a stage-three amendment."""
        return reported_sections(
            reading, signals, snapshot=snapshot, cad_alarm_level=cad_alarm_level
        )

    @staticmethod
    def unread(
        *, incident_id: str, channel: IntakeChannel, source_ref: str, reason: str
    ) -> IntakeReading:
        """The reading a caller gets when there was nothing to read."""
        return rejected_reading(
            incident_id=incident_id, channel=channel, source_ref=source_ref, reason=reason
        )

    @staticmethod
    def signals(reading: IntakeReading) -> IntakeSignals:
        return signals_from(reading)


__all__ = [
    "AGENT_ID",
    "AgentWaker",
    "IncidentInterceptor",
    "InterceptResult",
]
