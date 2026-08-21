"""The fleet runner: the one path by which a catalogued agent runs.

Before this existed, every agent was a plain object somebody called directly.
The registry described a fleet, the runtime enforced grants and deadlines, and
neither was on the path that actually did the work -- so the descriptor's
``required_scopes`` and ``latency_target_ms`` were documentation, and a run
record was written only where somebody remembered to write one.

``FleetRunner`` closes that. It resolves the pinned descriptor, mints the
standing grant that descriptor's scopes imply, opens a durable run record,
hands the work to :class:`~firstdue.ports.runtime.AgentRuntime`, and closes the
record with whatever terminal state came back. Four properties follow that no
agent has to remember:

* **No agent runs without a grant.** The runtime refuses before any work.
* **No agent runs past its declared budget.** The catalog's latency target is
  enforced, so it is a promise rather than a decoration.
* **Every run reaches a terminal state and is durable.** Including the denied
  and timed-out ones, which are the runs an investigation asks about.
* **Every run names the pinned version that produced it.** Which is the whole
  reason the registry pins versions.

The runner does not decide what an agent does. It decides that whatever an
agent does happens under an authority, inside a deadline, and on the record.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from firstdue.domain.enums import AgentRunStatus, Department, Loop, Scope
from firstdue.domain.registry import AgentDescriptor
from firstdue.domain.runs import AgentRunRecord
from firstdue.errors import ConfigurationError, NotFoundError
from firstdue.observability.logging import get_logger
from firstdue.ports.clock import Clock, IdGenerator
from firstdue.ports.repositories import AgentRunRepository, RegistryRepository
from firstdue.ports.runtime import (
    AgentHandler,
    AgentInput,
    AgentOutcome,
    AgentResult,
    AgentRuntime,
    Grant,
)
from firstdue.registry.descriptors import FLEET_VERSION, descriptor_for
from firstdue.services.grants import GrantService

logger = get_logger(__name__)

#: Which department each agent's standing grant is held by. An agent published
#: by one department and run by another still holds *its own* publisher's
#: authority -- that is what a pinned cross-department subscription means.
SUBSCRIBER_DEPARTMENT: Department = Department.FIRE


@dataclass(frozen=True, slots=True)
class FleetRun:
    """One agent run, as the caller sees it."""

    agent_id: str
    version: str
    result: AgentResult
    record: AgentRunRecord

    @property
    def completed(self) -> bool:
        return self.result.status is AgentRunStatus.COMPLETED

    @property
    def denied(self) -> bool:
        return self.result.status is AgentRunStatus.DENIED


class FleetRunner:
    """Runs catalogued agents, under grants, on the record."""

    def __init__(
        self,
        *,
        runtime: AgentRuntime,
        registry: RegistryRepository,
        grants: GrantService,
        runs: AgentRunRepository,
        clock: Clock,
        ids: IdGenerator,
        subscriber: Department = SUBSCRIBER_DEPARTMENT,
        only_agent: str = "",
    ) -> None:
        self._runtime = runtime
        self._registry = registry
        self._grants = grants
        self._runs = runs
        self._clock = clock
        self._ids = ids
        self._subscriber = subscriber
        # When this process is one agent's worker, it runs that agent and
        # refuses the rest. A worker that quietly ran work addressed to another
        # agent would execute it under the wrong service account, which is the
        # exact confusion the per-agent identities exist to prevent.
        self._only_agent = only_agent
        self._registered: set[str] = set()

    def register(self, agent_id: str, handler: AgentHandler) -> None:
        """Bind an agent id to the work it does.

        Delegated to the runtime rather than short-circuited here: the runtime
        owns the handler table, and it is the runtime that has to refuse a
        second, *different* implementation of one catalogued agent. Swallowing
        the second registration at this layer would hide exactly the
        misconfiguration the guard exists to surface.
        """
        self._runtime.register(agent_id, handler)
        self._registered.add(agent_id)

    def register_all(self, handlers: dict[str, AgentHandler]) -> None:
        for agent_id, handler in handlers.items():
            self.register(agent_id, handler)

    async def resolve(self, agent_id: str) -> AgentDescriptor:
        """The version this department has pinned, not the newest published.

        Publishing ``2.0.0`` does not move anybody's pin. Upgrading is a
        decision a department makes, and a run has to name the version that
        actually produced it.
        """
        pinned = await self._registry.resolve_pinned(self._subscriber, agent_id)
        if pinned is not None:
            return pinned
        # An unsubscribed agent falls back to the shipped descriptor rather
        # than refusing: the fleet's own agents run in processes that have not
        # seeded a subscription, and a poll that failed for that reason would
        # be a very confusing outage.
        try:
            return descriptor_for(agent_id, FLEET_VERSION)
        except NotFoundError:
            raise NotFoundError(
                "no descriptor for this agent",
                details={"agent_id": agent_id},
            ) from None

    async def run(
        self,
        agent_id: str,
        *,
        correlation_id: str,
        causation_id: str | None = None,
        parameters: dict[str, str] | None = None,
        ids: dict[str, str] | None = None,
        deadline: datetime | None = None,
        idempotency_key: str | None = None,
        grant: Grant | None = None,
    ) -> FleetRun:
        """Run one agent to a terminal state, on the record.

        A slow-loop agent runs under its standing grant, minted from the scopes
        its descriptor declares. An incident-loop agent does not: its authority
        is bound to one incident, one address, and one responding agency, and
        it expires at incident close -- so the caller has to supply it. Minting
        a standing grant for an incident agent would be a permanent authority
        where the whole design calls for a temporary one, and for the agents
        that reach EMS-derived facts the grant model refuses it outright.
        """
        if self._only_agent and agent_id != self._only_agent:
            raise ConfigurationError(
                "this process is another agent's worker and will not run this agent",
                details={"agent_id": agent_id, "worker_for": self._only_agent},
            )
        descriptor = await self.resolve(agent_id)
        if grant is None:
            if descriptor.loop is Loop.INCIDENT:
                raise ConfigurationError(
                    "an incident-loop agent runs under an incident grant, "
                    "which is bound to one incident and expires at its close",
                    details={"agent_id": descriptor.agent_id},
                )
            grant = await self._grants.standing_grant(
                agent_id,
                department=descriptor.publisher_department,
                scopes=frozenset(descriptor.required_scopes),
                correlation_id=correlation_id,
            )

        payload = AgentInput(
            correlation_id=correlation_id,
            causation_id=causation_id,
            ids=dict(ids or {}),
            parameters=dict(parameters or {}),
        )
        started = self._clock.now()
        run_id = self._ids.new_id("run")
        # The key is derived from what the run is *about*, so a duplicate
        # dispatch of the same tick is recognisable as the same work.
        key = idempotency_key or _derived_key(agent_id, correlation_id, payload)

        record = await self._runs.start(
            AgentRunRecord(
                run_id=run_id,
                agent_id=descriptor.agent_id,
                agent_version=descriptor.version,
                status=AgentRunStatus.RUNNING,
                correlation_id=correlation_id,
                causation_id=causation_id,
                idempotency_key=key,
                started_at=started,
            )
        )

        effective_deadline = deadline or _declared_deadline(descriptor, started)
        result = await self._runtime.invoke(descriptor, payload, grant, effective_deadline)

        finished = record.finished(
            result.status,
            at=self._clock.now(),
            error_code=result.error_code,
            error_message=result.error_message,
            written_fact_ids=result.written_fact_ids,
            emitted_event_ids=result.emitted_event_ids,
            write_action_ids=result.write_action_ids,
        )
        stored = await self._runs.save(finished)

        if result.status is not AgentRunStatus.COMPLETED:
            logger.warning(
                "agent_run_not_completed",
                extra={
                    "agent_ref": descriptor.ref,
                    "status": str(result.status),
                    "error_code": result.error_code,
                },
            )
        return FleetRun(
            agent_id=descriptor.agent_id,
            version=descriptor.version,
            result=result,
            record=stored,
        )


def _declared_deadline(descriptor: AgentDescriptor, started: datetime) -> datetime:
    """The descriptor's own latency target, as an instant."""
    return started + timedelta(milliseconds=descriptor.latency_target_ms)


def _derived_key(agent_id: str, correlation_id: str, payload: AgentInput) -> str:
    """An idempotency key derived from what the run is about.

    Derived rather than random, for the reason every other identifier in this
    system is derived: re-dispatching the same tick has to be recognisable as
    the same work rather than looking like new work that happens to match.
    """
    parts = [agent_id, correlation_id]
    parts.extend(f"{k}={v}" for k, v in sorted(payload.parameters.items()))
    parts.extend(f"{k}={v}" for k, v in sorted(payload.ids.items()))
    return ":".join(parts)[:200]


def outcome(
    *,
    facts: Sequence[str] = (),
    events: Sequence[str] = (),
    writes: Sequence[str] = (),
    decisions: Sequence[str] = (),
) -> AgentOutcome:
    """Build an outcome from whatever an agent's own result object carries."""
    return AgentOutcome(
        written_fact_ids=tuple(facts),
        emitted_event_ids=tuple(events),
        write_action_ids=tuple(writes),
        policy_decision_ids=tuple(decisions),
    )


def handler_returning(
    work: Callable[[], Awaitable[AgentOutcome]],
) -> AgentHandler:
    """Adapt a no-argument coroutine into a runtime handler.

    Most agents already have their own typed poll or rank method and do not
    need the grant handed to them -- the runtime checked it, and checking it
    twice in two places is how the two checks drift apart.
    """

    async def handler(_payload: AgentInput, _grant: object) -> AgentOutcome:
        return await work()

    return handler


__all__ = [
    "FleetRun",
    "FleetRunner",
    "Scope",
    "handler_returning",
    "outcome",
]
