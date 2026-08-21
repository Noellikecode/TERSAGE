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

from collections.abc import Mapping
from datetime import datetime

from firstdue.domain.registry import AgentDescriptor
from firstdue.ports.clock import Clock, IdGenerator
from firstdue.ports.runtime import AgentInput, AgentResult, AgentRunStatus, Grant


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
            written_fact_ids: tuple[str, ...] = (),
        ) -> AgentResult:
            return AgentResult(
                run_id=run_id,
                agent_ref=descriptor.ref,
                status=status,
                started_at=started,
                finished_at=self._clock.now(),
                written_fact_ids=written_fact_ids,
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

        return finish(AgentRunStatus.COMPLETED)

    @property
    def invocations(self) -> list[tuple[str, AgentInput]]:
        """Every (agent_ref, input) pair, in call order. Used by tests."""
        return list(self._invocations)
