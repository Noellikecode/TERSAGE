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

**The fifth thing, and why it is fifth.** Given somewhere to remember (a
:class:`~firstdue.services.memory_bank.MemoryBank`) or something to ask (a
:class:`~firstdue.ports.grounding.GroundingService`), this agent also composes
an :class:`~firstdue.incident.focus.IncidentFocus` -- what each woken agent
should look at first, in ids and canonical keys. That is where the weeks of slow
loop work finally reach a fireground: the conflict ``structure-watch`` opened in
March, the question ``hazard-watcher`` is still carrying, and the attribute the
caller just reported on, put beside each other and handed to the agents that
declared the authority to act on them. See
:mod:`firstdue.agents.graphs.interceptor`.

It is step five and not step two, and the ordering is the same argument the
intake's is. The instant brief has already been persisted and transmitted when
this starts, so a graph that is slow, confused, or absent costs a focus that
never arrives -- never a brief that never arrives. Nothing here can block, delay
or alter stage one, and ``BriefEmission`` refuses ``model_invoked=True`` on that
stage regardless (ADR 0004).

With neither collaborator wired -- the default, and what ``make demo`` and the
whole test suite run -- none of that happens and this agent behaves exactly as
it always has.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Final, Protocol

from pydantic import BaseModel, ConfigDict, Field

from firstdue.agents.graphs.base import (
    DEFAULT_MAX_STEPS,
    GraphCassette,
    GraphStop,
    ReasoningPlanner,
    graph_budget,
    run_graph,
)
from firstdue.domain.briefs import BriefEmission, BriefSection
from firstdue.domain.enums import Scope
from firstdue.domain.logentries import IncidentLogEntry
from firstdue.domain.memory import OpenQuestion
from firstdue.domain.profiles import ProfileSnapshot
from firstdue.domain.registry import AgentDescriptor
from firstdue.extraction.recorded import request_digest
from firstdue.incident.focus import IncidentFocus, focus_log_entry
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
from firstdue.ports.grounding import GroundingService
from firstdue.ports.repositories import IncidentLogRepository, RegistryRepository
from firstdue.registry.descriptors import active_descriptors
from firstdue.services.memory_bank import MemoryBank

if TYPE_CHECKING:
    from firstdue.agents.graphs.interceptor import FocusComposer

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
        incident_log: IncidentLogRepository | None = None,
        memory: MemoryBank | None = None,
        grounding: GroundingService | None = None,
        planner: ReasoningPlanner | None = None,
        traces: GraphCassette | None = None,
        use_langgraph: bool | None = None,
        max_graph_steps: int | None = None,
        agent_version: str = "1.0.0",
    ) -> None:
        self._intake = intake
        self._registry = registry
        self._waker = waker
        self._agent_id = agent_id
        self._log = incident_log
        # Optional, exactly as on the slow-loop watchers, and for the same
        # reason: with neither of them wired this agent runs the four steps it
        # has always run, byte for byte. Composing a focus is something a
        # deployment opts into by giving the incident loop somewhere to remember
        # and something to ask, not a change of behaviour it inherits.
        self._memory = memory
        self._grounding = grounding
        self._planner = planner
        self._traces = traces
        # Defaults to the built-in driver, unlike the slow-loop watchers, and
        # deliberately. ``run_graph`` raises ``ConfigurationError`` when
        # LangGraph is asked for and absent; a watcher can afford to discover
        # that on a nightly pass, and an incident on a six-second countdown
        # cannot. A deployment that has installed the extra turns it on with
        # ``settings.langgraph_enabled``, which is what every other graph caller
        # already passes.
        self._use_langgraph = bool(use_langgraph)
        self._max_graph_steps = max_graph_steps or DEFAULT_MAX_STEPS
        self._agent_version = agent_version

    @property
    def composes_focus(self) -> bool:
        """Whether this instance composes a focus at all.

        One predicate, read here and by nothing else, so "does this deployment
        reason about attention" has a single answer rather than two conditions
        that can drift apart.
        """
        return self._memory is not None or self._grounding is not None

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

    # ---------------------------------------------------------------- the focus

    async def compose_focus(
        self,
        *,
        incident_id: str,
        snapshot: ProfileSnapshot,
        now: datetime,
        reading: IntakeReading | None = None,
        authorised_scopes: frozenset[Scope] | None = None,
        descriptors: Sequence[AgentDescriptor] | None = None,
        unknown_keys: Sequence[str] = (),
        correlation_id: str = "",
        deadline: datetime | None = None,
    ) -> IncidentFocus | None:
        """What each woken agent should look at first. Never raises, never parks.

        Called *after* the instant brief is on the record. Returns ``None`` when
        this deployment wired neither a memory bank nor a grounding service --
        the default, and the configuration the existing suite runs in.

        Three properties, and every one of them is about what happens when this
        goes wrong rather than when it goes right, because an incident is
        already underway by the time it is called:

        * **The budget is the descriptor's**, through
          :func:`~firstdue.agents.graphs.base.graph_budget` against the
          catalogued ``latency_target_ms`` of six seconds, and it is checked in
          the graph's router. A graph that reaches either ceiling parks.
        * **Exhaustion produces a focus anyway.** The park path falls through to
          :func:`~firstdue.agents.graphs.interceptor.fallback_focus`, which
          ranks the profile's own open conflicts and the questions the recall
          node already got back. The planner is the only thing lost.
        * **Nothing here propagates.** A missing LangGraph, a planner that
          raises, a bank that times out -- all of it is caught, logged with an
          error type and no message, and answered with the deterministic focus.
          An incident cannot wait for a graph and must never be handed nothing
          because one was slow.

        ``authorised_scopes`` is the incident grant's own scope set and it is the
        gate on durable memory: a Tier II thread reaches this incident only if
        this incident's grant carries the Tier II scope. Passing nothing recalls
        nothing, which is the safe direction to be wrong in.
        """
        if not self.composes_focus:
            return None

        # Imported here rather than at module scope, and for a structural
        # reason rather than an optional-dependency one. ``incident/focus.py``
        # is the contract the graph is written against, and ``incident/__init__``
        # re-exports this class -- so a module-scope import from here into the
        # graph closes a loop through the package. Resolving it at call time is
        # the same discipline ``container.py`` uses for the Google clients, and
        # the two graph modules the other incident agents grew do the same.
        from firstdue.agents.graphs.interceptor import (
            FocusComposer,
            FocusGraphState,
            graph_focus,
        )

        catalog = descriptors if descriptors is not None else await self._catalog()
        reported_keys = reading.reported_keys if reading is not None else ()
        scopes = authorised_scopes or frozenset()
        # Named outside the try so the fallback below can still reach whatever
        # the recall node got back before the graph came apart. A run that read
        # the bank and then lost its planner has not lost the questions.
        composer: FocusComposer | None = None

        try:
            budget = graph_budget(
                self._agent_id,
                deadline=deadline,
                started=now,
                max_steps=self._max_graph_steps,
            )
            composer = FocusComposer(
                snapshot=snapshot,
                budget=budget,
                memory=self._memory,
                scopes=scopes,
                planner=self._planner,
            )
            digest = request_digest("incident-focus", incident_id, snapshot.snapshot_id)
            run = await run_graph(
                composer.spec(),
                FocusGraphState(
                    district_id=snapshot.district_id,
                    correlation_id=correlation_id,
                    incident_id=incident_id,
                    address_id=snapshot.address_id,
                    reported_keys=tuple(reported_keys),
                    unknown_keys=tuple(unknown_keys),
                ),
                agent_id=self._agent_id,
                agent_version=self._agent_version,
                budget=budget,
                request_digest=digest,
                use_langgraph=self._use_langgraph,
                recorded=self._traces.load(digest) if self._traces is not None else None,
            )
            if self._traces is not None:
                self._traces.store(run.trace)

            if run.trace.stop is GraphStop.CLOSED:
                focus = graph_focus(
                    run.state,
                    composer=composer,
                    snapshot=snapshot,
                    descriptors=catalog,
                    self_agent_id=self._agent_id,
                    composed_by_version=self._agent_version,
                    composed_at=now,
                )
            else:
                # Parked. The recall node's questions are still on the composer,
                # so the fallback is composed against everything this run did
                # manage to read -- it is not a restart from nothing.
                focus = self._fallback(
                    incident_id=incident_id,
                    snapshot=snapshot,
                    descriptors=catalog,
                    questions=composer.questions,
                    reported_keys=reported_keys,
                    unknown_keys=unknown_keys,
                    now=now,
                )
            logger.info(
                "focus_composed",
                extra={
                    "incident_id": incident_id,
                    "profile_version": focus.profile_version,
                    "stop": str(run.trace.stop),
                    "steps": len(run.trace.records),
                    "agents": ",".join(focus.agent_ids),
                    "pointers": focus.pointer_count,
                },
            )
            return focus
        except Exception as exc:
            logger.warning(
                "focus_graph_failed",
                extra={"incident_id": incident_id, "error_type": type(exc).__name__},
            )

        try:
            return self._fallback(
                incident_id=incident_id,
                snapshot=snapshot,
                descriptors=catalog,
                questions=composer.questions if composer is not None else (),
                reported_keys=reported_keys,
                unknown_keys=unknown_keys,
                now=now,
            )
        except Exception as exc:  # pragma: no cover - the fallback is pure
            logger.warning(
                "focus_unavailable",
                extra={"incident_id": incident_id, "error_type": type(exc).__name__},
            )
            return None

    def _fallback(
        self,
        *,
        incident_id: str,
        snapshot: ProfileSnapshot,
        descriptors: Sequence[AgentDescriptor],
        questions: Sequence[OpenQuestion],
        reported_keys: Sequence[str],
        unknown_keys: Sequence[str],
        now: datetime,
    ) -> IncidentFocus:
        from firstdue.agents.graphs.interceptor import fallback_focus

        return fallback_focus(
            incident_id=incident_id,
            snapshot=snapshot,
            descriptors=descriptors,
            questions=questions,
            reported_keys=reported_keys,
            unknown_keys=unknown_keys,
            self_agent_id=self._agent_id,
            composed_by_version=self._agent_version,
            composed_at=now,
        )

    async def record_focus(self, focus: IncidentFocus, *, now: datetime) -> IncidentLogEntry | None:
        """Append the focus to the incident log. The only place it is stored.

        The log, rather than a collection of its own, because the log is already
        append-only, gapless, sealable, replayable and written through to the
        records system -- and "what was this agent told to look at" is exactly
        the question it exists to answer afterwards. Returns ``None`` when no log
        was wired, which is the configuration a routing unit test runs in.
        """
        if self._log is None:
            return None
        sequence = await self._log.next_sequence(focus.incident_id)
        return await self._log.append(focus_log_entry(focus, sequence=sequence, now=now))

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
