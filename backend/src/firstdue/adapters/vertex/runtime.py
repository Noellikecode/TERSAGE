"""ADKRuntime -- agents on Vertex AI, refusing exactly what the fake refuses.

The port's docstring has named this class since phase 1: *"``ADKRuntime``
(Google ADK on Vertex AI) and ``FakeRuntime`` (deterministic, credential-free)
both satisfy this."* The rules it enforces are not new here, and that is the
point -- a parity test asserts this runtime denies precisely what the fake
denies, so the credential-free demo remains a faithful rehearsal:

* a grant that has expired or been revoked is **DENIED**, before any work;
* a grant missing any required scope is **DENIED**;
* an elapsed deadline is **TIMED_OUT**;
* every run reaches a terminal state -- no agent stays running forever.

One difference, deliberately: :class:`FakeRuntime` checks the deadline only at
the start, because it has no work to bound. This one propagates the deadline
through the call and cancels, so an agent that hangs against a slow dependency
is timed out rather than holding an instance until Cloud Run kills it.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any

from firstdue.domain.enums import AgentRunStatus
from firstdue.domain.registry import AgentDescriptor
from firstdue.errors import ConfigurationError
from firstdue.observability.logging import get_logger
from firstdue.observability.tracing import agent_span
from firstdue.ports.clock import Clock, IdGenerator
from firstdue.ports.runtime import (
    AgentHandler,
    AgentInput,
    AgentOutcome,
    AgentResult,
    Grant,
)
from firstdue.reliability.budget import budget_seconds

logger = get_logger(__name__)

#: What an agent gets if the descriptor names no budget. Every descriptor does.


class ADKRuntime:
    """Runs one agent to a terminal state on Vertex AI."""

    def __init__(
        self,
        *,
        clock: Clock,
        ids: IdGenerator,
        project_id: str,
        location: str,
        agent_engine: Any | None = None,
    ) -> None:
        if not project_id:
            raise ConfigurationError("the agent runtime requires GCP_PROJECT_ID")
        self._clock = clock
        self._ids = ids
        self._project_id = project_id
        self._location = location
        self._engine = agent_engine
        self.invocations: list[tuple[str, AgentInput]] = []
        self._handlers: dict[str, AgentHandler] = {}

    def register(self, agent_id: str, handler: AgentHandler) -> None:
        """Bind an agent id to the work it does."""
        existing = self._handlers.get(agent_id)
        if existing is not None and existing is not handler:
            raise ConfigurationError(
                "an agent id already has a different handler; two implementations "
                "of one catalogued agent means the catalog no longer describes "
                "what runs",
                details={"agent_id": agent_id},
            )
        self._handlers[agent_id] = handler

    async def invoke(
        self,
        descriptor: AgentDescriptor,
        payload: AgentInput,
        grant: Grant,
        deadline: datetime | None = None,
    ) -> AgentResult:
        """Run one agent under one grant, within one deadline."""
        started = self._clock.now()
        run_id = self._ids.new_id("run")
        self.invocations.append((descriptor.ref, payload))

        def finish(
            status: AgentRunStatus,
            *,
            error_code: str | None = None,
            error_message: str | None = None,
            outcome: AgentOutcome | None = None,
        ) -> AgentResult:
            produced = outcome or AgentOutcome()
            return AgentResult(
                run_id=run_id,
                agent_ref=descriptor.ref,
                status=status,
                started_at=started,
                finished_at=self._clock.now(),
                written_fact_ids=produced.written_fact_ids,
                emitted_event_ids=produced.emitted_event_ids,
                write_action_ids=produced.write_action_ids,
                policy_decision_ids=produced.policy_decision_ids,
                error_code=error_code,
                error_message=error_message,
            )

        # Authorization first, before any work -- exactly as the fake does, and
        # in the same order, so the parity test is meaningful.
        if grant.is_expired(started):
            return finish(
                AgentRunStatus.DENIED,
                error_code="GRANT_EXPIRED",
                error_message="grant is expired or revoked",
            )
        missing = descriptor.required_scopes - grant.scopes
        if missing:
            logger.warning(
                "agent_run_denied",
                extra={"agent_ref": descriptor.ref, "missing_scopes": len(missing)},
            )
            return finish(
                AgentRunStatus.DENIED,
                error_code="NOT_AUTHORIZED",
                error_message="grant does not carry every scope this agent requires",
            )
        if deadline is not None and started >= deadline:
            return finish(
                AgentRunStatus.TIMED_OUT,
                error_code="UPSTREAM_TIMEOUT",
                error_message="deadline elapsed before the run started",
            )

        budget_s = budget_seconds(descriptor, deadline, started)

        with agent_span(
            descriptor.agent_id,
            agent_version=descriptor.version,
            run_id=run_id,
            correlation_id=payload.correlation_id,
            grant_id=grant.grant_id,
        ) as span:
            span.set("deadline_ms", int(budget_s * 1000))
            began = time.perf_counter()
            outcome: AgentOutcome | None = None
            try:
                async with asyncio.timeout(budget_s):
                    outcome = await self._run(descriptor, payload, grant)
            except TimeoutError:
                span.set("timed_out", True)
                span.set("latency_ms", round((time.perf_counter() - began) * 1000, 3))
                return finish(
                    AgentRunStatus.TIMED_OUT,
                    error_code="UPSTREAM_TIMEOUT",
                    error_message="the run exceeded its deadline and was cancelled",
                )
            except asyncio.CancelledError:
                # A cancelled run is still a run that ended. Recording it as
                # CANCELLED rather than letting it disappear is what keeps
                # "every run reaches a terminal state" true under shutdown.
                span.set("cancelled", True)
                return finish(
                    AgentRunStatus.CANCELLED,
                    error_code="CANCELLED",
                    error_message="the run was cancelled before it finished",
                )
            except Exception as exc:
                from firstdue.reliability.retry import error_code_of

                code = error_code_of(exc)
                span.set_rejected(code)
                logger.warning(
                    "agent_run_failed",
                    extra={"agent_ref": descriptor.ref, "error_code": code},
                )
                # The message is a stable code, never the exception text: a
                # traceback can carry record contents.
                return finish(
                    AgentRunStatus.FAILED,
                    error_code=code,
                    error_message=f"the run failed with {code}",
                )

            span.set("latency_ms", round((time.perf_counter() - began) * 1000, 3))
            return finish(AgentRunStatus.COMPLETED, outcome=outcome)

    async def _run(
        self, descriptor: AgentDescriptor, payload: AgentInput, grant: Grant
    ) -> AgentOutcome | None:
        """Run the agent's registered work, then notify the engine.

        The handler is the agent. The engine -- Vertex Agent Engine, when one is
        configured -- is told the run happened so a managed session records it;
        it is not what does the work, and a process without one runs the same
        fleet. That ordering matters: an engine outage must not stop a district
        poll, because the poll is what the department's readiness depends on.
        """
        handler = self._handlers.get(descriptor.agent_id)
        outcome = await handler(payload, grant) if handler is not None else None

        if self._engine is not None:  # pragma: no cover - live mode only
            await asyncio.to_thread(
                self._engine.run,
                agent_ref=descriptor.ref,
                correlation_id=payload.correlation_id,
                ids=dict(payload.ids),
                parameters=dict(payload.parameters),
            )
        return outcome
