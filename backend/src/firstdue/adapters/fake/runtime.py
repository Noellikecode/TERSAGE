"""FakeRuntime -- the credential-free agent runtime.

It is deterministic, but it is not permissive. It enforces the same rules the
ADK runtime will:

* an agent whose grant lacks a required scope is **DENIED**, not run;
* an expired or revoked grant is **DENIED**;
* a past deadline is **TIMED_OUT**;
* every run reaches a terminal state -- no agent stays running forever.

Scripted outcomes let tests exercise failure paths without pretending failures
cannot happen.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime

from firstdue.domain.registry import AgentDescriptor
from firstdue.errors import ConfigurationError
from firstdue.ports.clock import Clock, IdGenerator
from firstdue.ports.runtime import (
    AgentHandler,
    AgentInput,
    AgentOutcome,
    AgentResult,
    AgentRunStatus,
    Grant,
)
from firstdue.reliability.budget import budget_seconds


class FakeRuntime:
    """Deterministic agent runtime for fake mode."""

    def __init__(
        self,
        *,
        clock: Clock,
        ids: IdGenerator,
        scripted_failures: Mapping[str, str] | None = None,
        scripted_timeouts: frozenset[str] = frozenset(),
    ) -> None:
        self._clock = clock
        self._ids = ids
        self._failures = dict(scripted_failures or {})
        self._timeouts = set(scripted_timeouts)
        self._invocations: list[tuple[str, AgentInput]] = []
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
        started = self._clock.now()
        self._invocations.append((descriptor.ref, payload))
        run_id = self._ids.new_id("run")

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

        # Authorization is checked before any work, exactly as in live mode.
        if grant.is_expired(started):
            return finish(
                AgentRunStatus.DENIED,
                error_code="GRANT_EXPIRED",
                error_message="grant is expired or revoked",
            )
        missing = descriptor.required_scopes - grant.scopes
        if missing:
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
        if descriptor.agent_id in self._timeouts:
            return finish(
                AgentRunStatus.TIMED_OUT,
                error_code="UPSTREAM_TIMEOUT",
                error_message="scripted timeout",
            )
        if descriptor.agent_id in self._failures:
            return finish(
                AgentRunStatus.FAILED,
                error_code="INTERNAL_ERROR",
                error_message=self._failures[descriptor.agent_id],
            )

        handler = self._handlers.get(descriptor.agent_id)
        if handler is None:
            # A catalogued agent with nothing behind it completes having done
            # nothing, which is what the registry-only tests rely on. It is not
            # an error: publishing a descriptor and wiring its work are
            # separate acts, and the console shows the difference.
            return finish(AgentRunStatus.COMPLETED)

        budget_s = budget_seconds(descriptor, deadline, started)
        try:
            async with asyncio.timeout(budget_s):
                outcome = await handler(payload, grant)
        except TimeoutError:
            return finish(
                AgentRunStatus.TIMED_OUT,
                error_code="UPSTREAM_TIMEOUT",
                error_message="the run exceeded its deadline and was cancelled",
            )
        except asyncio.CancelledError:
            return finish(
                AgentRunStatus.CANCELLED,
                error_code="CANCELLED",
                error_message="the run was cancelled before it finished",
            )
        except Exception as exc:
            from firstdue.reliability.retry import error_code_of

            code = error_code_of(exc)
            # The message is a stable code, never the exception text: a
            # traceback can carry record contents.
            return finish(
                AgentRunStatus.FAILED,
                error_code=code,
                error_message=f"the run failed with {code}",
            )

        return finish(AgentRunStatus.COMPLETED, outcome=outcome)

    @property
    def invocations(self) -> list[tuple[str, AgentInput]]:
        """Every (agent_ref, input) pair, in call order. Used by tests."""
        return list(self._invocations)
